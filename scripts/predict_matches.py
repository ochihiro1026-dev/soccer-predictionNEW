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


def generate_ai_analysis(gemini_key: str, match: dict) -> str:
    if not gemini_key:
        return ""
    prompt = f"""あなたは海外サッカーの予想アナリストです。以下の試合についてGoogle検索で最新のチーム情報（怪我人・出場停止・復帰・監督交代・ミッドウィーク日程・過密日程）を確認し、13項目の予想ルールに沿って日本語で簡潔に分析してください。

【試合データ】
・大会 / 日時: {match['league']} / {match['kickoff_jst']} JST
・対戦: {match['home']} vs {match['away']}
・本命（出し）: {match['favorite_team']}（{match['favorite_side']}）
・勝率(上乗せ除去後): {match['fair_prob_pct']['fav']}% / 推定ハンデ: {match['estimated_handicap']} / 期待値: {match['metrics_at_est']['ev_pct']:+.1f}%
・オッズ幅: {match['fav_odds_range']}
・注意タグ: {', '.join(match['alerts']) if match['alerts'] else 'なし'}

【出力ルール（表は使わず、箇条書きのみ・150〜220文字程度）】
・欠場/復帰：主力選手の欠場・出場停止・復帰（未確認や1媒体のみの場合はその旨を明記）
・日程/監督：休養日数やミッドウィーク試合、監督・環境の注意点
・判定メモ：出し中心の方針・大勝チェック・逆の例外条件に照らした一言（確定情報がない見出しだけの噂は根拠にしない）"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        parts = res.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return text
    except Exception as e:
        print(f"[WARN] Gemini API error ({match['favorite_team']}): {e}")
        return ""


def analyze_event(ev: dict, league_name: str, standings: dict, forms: dict):
    home = ev.get("home_team", "")
    away = ev.get("away_team", "")
    bookmakers = ev.get("bookmakers", [])
    if not bookmakers:
        return None

    home_odds, draw_odds, away_odds = [], [], []
    for bm in bookmakers:
        for m in bm.get("markets", []):
            if m.get("key") != "h2h":
                continue
            outcomes = {o["name"]: o["price"] for o in m.get("outcomes", [])}
            if home in outcomes and away in outcomes and "Draw" in outcomes:
                home_odds.append(outcomes[home])
                draw_odds.append(outcomes["Draw"])
                away_odds.append(outcomes[away])

    if not home_odds:
        return None

    avg_h = sum(home_odds) / len(home_odds)
    avg_d = sum(draw_odds) / len(draw_odds)
    avg_a = sum(away_odds) / len(away_odds)

    if avg_h <= avg_a:
        fav_side, fav_team = "HOME", home
        o_fav, o_draw, o_dog = avg_h, avg_d, avg_a
        fav_min, fav_max = min(home_odds), max(home_odds)
    else:
        fav_side, fav_team = "AWAY", away
        o_fav, o_draw, o_dog = avg_a, avg_d, avg_h
        fav_min, fav_max = min(away_odds), max(away_odds)

    fav_range = f"{fav_min:.2f}〜{fav_max:.2f}"
    raw_f, raw_d, raw_l = 1.0 / o_fav, 1.0 / o_draw, 1.0 / o_dog
    total_raw = raw_f + raw_d + raw_l
    p_fav, p_draw, p_dog = raw_f / total_raw, raw_d / total_raw, raw_l / total_raw

    diff_probs = estimate_goal_diff_probs(p_fav, p_draw, p_dog)

    best_h = HANDICAPS[0]
    best_metrics = None
    min_abs_ev = 9999.0
    for h_id in HANDICAPS:
        m = calc_metrics(h_id, diff_probs)
        if abs(m["ev_pct"]) < min_abs_ev:
            min_abs_ev = abs(m["ev_pct"])
            best_h = h_id
            best_metrics = m

    win_pct = round(p_fav * 100, 1)
    kickoff_jst = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(JST)

    alerts = []
    if league_name == "CL":
        alerts.append("CL: 本命優位傾向")
    elif league_name == "EL":
        alerts.append("EL: CLより波乱注意")

    hhmm = kickoff_jst.hour * 60 + kickoff_jst.minute
    if 19 * 60 <= hhmm <= 20 * 60 + 30:
        alerts.append("19:00〜20:30開始（崩れ注意・暫定）")

    if 45.0 <= win_pct <= 55.0 and HANDICAPS.index(best_h) >= HANDICAPS.index("0半"):
        alerts.append("勝率5割前後・引分け全負け注意")

    if (fav_max - fav_min) >= 0.15:
        alerts.append("業者間オッズ差大")

    h_stat = find_team_stat(standings, home)
    a_stat = find_team_stat(standings, away)
    h_form = find_team_form(forms, home)
    a_form = find_team_form(forms, away)
    home_label = format_team_label(home, h_stat, h_form)
    away_label = format_team_label(away, a_stat, a_form)

    blowout_score = None
    if h_stat and a_stat:
        fav_st = h_stat if fav_side == "HOME" else a_stat
        dog_st = a_stat if fav_side == "HOME" else h_stat
        cnt = 0
        if fav_st["gf_avg"] >= 1.8:
            cnt += 1
        if fav_st["ga_avg"] <= 1.0:
            cnt += 1
        if dog_st["gf_avg"] <= 1.0:
            cnt += 1
        if dog_st["ga_avg"] >= 1.6:
            cnt += 1
        blowout_score = f"{cnt}/4項目(休養差除く)"

    if win_pct >= 62 and best_metrics["get_pct"] >= 60:
        stars = 4
        rec_type = "確信型候補"
    elif best_metrics["get_pct"] >= 58 and best_metrics["ev_pct"] >= -5.0:
        stars = 3
        rec_type = "的中率型候補"
    else:
        stars = 2
        rec_type = "参考（ハンデ確認要）"

    if league_name == "EL" and stars >= 4:
        stars = 3

    return {
        "id": ev.get("id"),
        "league": league_name,
        "kickoff_jst": kickoff_jst.strftime("%Y-%m-%d %H:%M"),
        "home": home_label,
        "away": away_label,
        "favorite_side": fav_side,
        "favorite_team": fav_team,
        "odds_avg": {"fav": round(o_fav, 2), "draw": round(o_draw, 2), "dog": round(o_dog, 2)},
        "fav_odds_range": fav_range,
        "fair_prob_pct": {"fav": win_pct, "draw": round(p_draw * 100, 1), "dog": round(p_dog * 100, 1)},
        "estimated_handicap": best_h,
        "metrics_at_est": best_metrics,
        "preliminary_type": rec_type,
        "preliminary_stars": stars,
        "alerts": alerts,
        "blowout_auto": blowout_score,
        "ai_analysis": "",
        "bookmaker_count": len(home_odds),
    }


def notify_discord(webhook_url: str, payload: dict, site_url: str):
    if not webhook_url:
        return
    matches = payload.get("matches", [])
    star4 = [m for m in matches if m.get("preliminary_stars", 0) >= 4]
    lines = [
        f"⚽ **海外サッカー自動予想を更新しました**（{payload['generated_at_jst']}）",
        f"対象: {payload['match_count']}試合（{payload['window_jst']}）",
    ]
    if star4:
        lines.append("【★4候補】")
        for m in star4[:5]:
            ev = m["metrics_at_est"]["ev_pct"]
            lines.append(
                f"・{m['home']} vs {m['away']}（出し: {m['favorite_team']} / 推定: {m['estimated_handicap']} / EV: {ev:+.1f}%）"
            )
    else:
        lines.append("【★4候補】該当なし")

    if site_url:
        lines.append(f"▼ サイトで確認する\n{site_url}")

    body = json.dumps({"content": "\n".join(lines)}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "SoccerPredictorBot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Discord notified: {resp.status}")
    except Exception as e:
        print(f"[WARN] Discord notification failed: {e}")


def main():
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("環境変数 ODDS_API_KEY が設定されていません。")
    fd_key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    site_url = os.environ.get("SITE_URL", "").strip()

    now_jst = datetime.now(JST)
    start_jst = now_jst.replace(hour=19, minute=0, second=0, microsecond=0)
    end_jst = (start_jst + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    from_iso = start_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = end_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    results = []
    for sport_key, league_name, fd_code in TARGET_LEAGUES:
        events = fetch_league_odds(api_key, sport_key, from_iso, to_iso)
        standings = {}
        forms = {}
        if events and fd_key and fd_code:
            standings = fetch_standings(fd_key, fd_code)
            forms = fetch_recent_forms(fd_key, fd_code)
            time.sleep(1.5)
        for ev in events:
            parsed = analyze_event(ev, league_name, standings, forms)
            if parsed:
                results.append(parsed)

    results.sort(key=lambda x: (-x["preliminary_stars"], x["kickoff_jst"]))

    if gemini_key:
        for m in results[:5]:
            m["ai_analysis"] = generate_ai_analysis(gemini_key, m)
            time.sleep(4.0)

    os.makedirs("data", exist_ok=True)
    payload = {
        "generated_at_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "window_jst": f"{start_jst.strftime('%Y-%m-%d %H:%M')} 〜 {end_jst.strftime('%Y-%m-%d %H:%M')}",
        "match_count": len(results),
        "matches": results,
    }
    with open("data/predictions.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Updated data/predictions.json ({len(results)} matches)")
    notify_discord(webhook_url, payload, site_url)


if __name__ == "__main__":
    main()
