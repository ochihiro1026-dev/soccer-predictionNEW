import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

TARGET_LEAGUES = [
    ("soccer_epl", "プレミアリーグ", "PL"),
    ("soccer_spain_la_liga", "ラ・リーガ", "PD"),
    ("soccer_italy_serie_a", "セリエA", "SA"),
    ("soccer_germany_bundesliga", "ブンデスリーガ", "BL1"),
    ("soccer_france_ligue_one", "リーグ・アン", "FL1"),
    ("soccer_uefa_champs_league", "CL", "CL"),
    ("soccer_uefa_europa_league", "EL", None),
]

HANDICAPS = [
    "0", "0/3", "0/5", "0/7", "0/8", "0半",
    "0半3", "0半5", "0半7", "1.0", "1.2", "1.3",
    "1.5", "1.7", "1半", "1半7", "2.0", "2.4", "3.0", "3半4",
]

PAYOUT_RATE = 1.95
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash"]


def eval_outcome(h_id: str, diff: int):
    if h_id == "0":
        return ("fullWin", 1.0) if diff >= 1 else ("push", 0.0) if diff == 0 else ("fullLose", -1.0)
    if h_id.startswith("0/"):
        k = int(h_id.split("/")[1]) / 10.0
        return ("fullWin", 1.0) if diff >= 1 else ("partLose", -k) if diff == 0 else ("fullLose", -1.0)
    if h_id == "0半":
        return ("fullWin", 1.0) if diff >= 1 else ("fullLose", -1.0)
    if h_id.startswith("0半"):
        k = (10 - int(h_id.replace("0半", ""))) / 10.0
        return ("fullWin", 1.0) if diff >= 2 else ("partWin", k) if diff == 1 else ("fullLose", -1.0)
    if h_id in ("1.0", "1.2", "1.3", "1.5", "1.7"):
        k = int(h_id.split(".")[1]) / 10.0
        if diff >= 2:
            return ("fullWin", 1.0)
        if diff == 1:
            return ("push", 0.0) if k == 0 else ("partLose", -k)
        return ("fullLose", -1.0)
    if h_id == "1半":
        return ("fullWin", 1.0) if diff >= 2 else ("fullLose", -1.0)
    if h_id == "1半7":
        return ("fullWin", 1.0) if diff >= 3 else ("partWin", 0.3) if diff == 2 else ("fullLose", -1.0)
    if h_id in ("2.0", "2.4"):
        k = int(h_id.split(".")[1]) / 10.0
        if diff >= 3:
            return ("fullWin", 1.0)
        if diff == 2:
            return ("push", 0.0) if k == 0 else ("partLose", -k)
        return ("fullLose", -1.0)
    if h_id == "3.0":
        return ("fullWin", 1.0) if diff >= 4 else ("push", 0.0) if diff == 3 else ("fullLose", -1.0)
    if h_id == "3半4":
        return ("fullWin", 1.0) if diff >= 5 else ("partWin", 0.6) if diff == 4 else ("fullLose", -1.0)
    return ("fullLose", -1.0)


def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def estimate_goal_diff_probs(p_win: float, p_draw: float, p_lose: float) -> dict:
    total_goals = 2.65
    best_err = 999.0
    best_lh, best_la = 1.4, 1.2
    for i in range(40):
        lh = 0.4 + i * 0.08
        la = max(0.25, total_goals - lh * 0.78)
        w = d = l = 0.0
        for h in range(8):
            ph = poisson_pmf(h, lh)
            for a in range(8):
                p = ph * poisson_pmf(a, la)
                if h > a:
                    w += p
                elif h == a:
                    d += p
                else:
                    l += p
        err = (w - p_win) ** 2 + (d - p_draw) ** 2 + (l - p_lose) ** 2
        if err < best_err:
            best_err = err
            best_lh, best_la = lh, la

    raw_d = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
    for h in range(9):
        ph = poisson_pmf(h, best_lh)
        for a in range(9):
            if h > a:
                diff = min(5, h - a)
                raw_d[diff] += ph * poisson_pmf(a, best_la)

    sum_win = sum(raw_d.values())
    scale = (p_win / sum_win) if sum_win > 0 else 0.0
    return {
        "lose": p_lose,
        0: p_draw,
        1: raw_d[1] * scale,
        2: raw_d[2] * scale,
        3: raw_d[3] * scale,
        4: raw_d[4] * scale,
        5: raw_d[5] * scale,
    }


def calc_metrics(h_id: str, diff_probs: dict, payout_rate: float = PAYOUT_RATE) -> dict:
    diffs = [(-1, diff_probs["lose"]), (0, diff_probs[0]), (1, diff_probs[1]),
             (2, diff_probs[2]), (3, diff_probs[3]), (4, diff_probs[4]), (5, diff_probs[5])]
    full_win = get_prob = ev = 0.0
    profit_unit = payout_rate - 1.0
    for d, p in diffs:
        outcome, ratio = eval_outcome(h_id, d)
        if outcome == "fullWin":
            full_win += p
            get_prob += p
            ev += p * (ratio * profit_unit)
        elif outcome == "partWin":
            get_prob += p
            ev += p * (ratio * profit_unit)
        elif outcome in ("partLose", "fullLose"):
            ev += p * ratio
    return {
        "full_win_pct": round(full_win * 100, 1),
        "get_pct": round(get_prob * 100, 1),
        "ev_pct": round(ev * 100, 1),
    }


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def fetch_standings(fd_key: str, comp_code: str) -> dict:
    if not fd_key or not comp_code:
        return {}
    url = f"https://api.football-data.org/v4/competitions/{comp_code}/standings"
    req = urllib.request.Request(url, headers={"X-Auth-Token": fd_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        teams = {}
        form_map = {"W": "勝", "D": "分", "L": "負"}
        for st in data.get("standings", []):
            if st.get("type") != "TOTAL":
                continue
            for row in st.get("table", []):
                t = row.get("team", {})
                name = t.get("shortName") or t.get("name", "")
                played = max(1, row.get("playedGames", 1))
                raw_form = row.get("form") or ""
                form_jp = "".join(form_map.get(x.strip(), "") for x in raw_form.split(","))
                teams[norm_name(name)] = {
                    "pos": row.get("position"),
                    "gf": row.get("goalsFor", 0),
                    "ga": row.get("goalsAgainst", 0),
                    "form": form_jp,
                    "gf_avg": round(row.get("goalsFor", 0) / played, 2),
                    "ga_avg": round(row.get("goalsAgainst", 0) / played, 2),
                }
        return teams
    except Exception as e:
        print(f"[WARN] standings {comp_code}: {e}")
        return {}


def fetch_recent_forms(fd_key: str, comp_code: str) -> dict:
    if not fd_key or not comp_code:
        return {}
    url = f"https://api.football-data.org/v4/competitions/{comp_code}/matches?status=FINISHED"
    req = urllib.request.Request(url, headers={"X-Auth-Token": fd_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        matches = sorted(data.get("matches", []), key=lambda m: m.get("utcDate", ""))
        history = {}
        for m in matches:
            ft = m.get("score", {}).get("fullTime", {})
            hg, ag = ft.get("home"), ft.get("away")
            if hg is None or ag is None:
                continue
            ht = m.get("homeTeam", {})
            at = m.get("awayTeam", {})
            h_key = norm_name(ht.get("shortName") or ht.get("name", ""))
            a_key = norm_name(at.get("shortName") or at.get("name", ""))
            if hg > ag:
                h_res, a_res = "勝", "負"
            elif hg < ag:
                h_res, a_res = "負", "勝"
            else:
                h_res, a_res = "分", "分"
            if h_key:
                history.setdefault(h_key, []).append(h_res)
            if a_key:
                history.setdefault(a_key, []).append(a_res)
        return {k: "".join(v[-5:]) for k, v in history.items()}
    except Exception as e:
        print(f"[WARN] recent_forms {comp_code}: {e}")
        return {}


def find_team_stat(standings: dict, team_name: str):
    key = norm_name(team_name)
    if key in standings:
        return standings[key]
    for k, v in standings.items():
        if k in key or key in k:
            return v
    return None


def find_team_form(forms: dict, team_name: str) -> str:
    key = norm_name(team_name)
    if key in forms:
        return forms[key]
    for k, v in forms.items():
        if k in key or key in k:
            return v
    return ""


def format_team_label(name: str, stat, form_str: str) -> str:
    if not stat or not stat.get("pos"):
        return f"{name} [直近:{form_str}]" if form_str else name
    parts = [f"{stat['pos']}位", f"得点{stat['gf']}/失点{stat['ga']}"]
    f_val = form_str or stat.get("form", "")
    if f_val:
        parts.append(f"直近:{f_val}")
    return f"{name} [{' '.join(parts)}]"


def fetch_league_odds(api_key: str, sport_key: str, from_iso: str, to_iso: str) -> list:
    params = urllib.parse.urlencode({
        "apiKey": api_key,
        "regions": "uk,eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "commenceTimeFrom": from_iso,
        "commenceTimeTo": to_iso,
    })
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "SoccerPredictorBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] {sport_key}: {e}")
        return []


def call_gemini(gemini_key: str, model: str, prompt: str, use_search: bool) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
    payload_dict = {"contents": [{"parts": [{"text": prompt}]}]}
    if use_search:
        payload_dict["tools"] = [{"google_search": {
