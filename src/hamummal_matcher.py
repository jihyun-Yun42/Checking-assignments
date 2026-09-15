import re

_ROUND_RE = re.compile(r"\((\d+)\s*회?\s*차?\)\s*$")
_NOISE_RE = re.compile(
    r"[❤️💙\U0001F300-\U0001FAFF☀-➿️]"
)

_SURNAME_STOPWORDS = {
    "kim", "lee", "park", "choi", "choe", "jung", "jeong", "cho", "jo",
    "kang", "kwon", "hwang", "song", "ahn", "an", "yoo", "yu", "ryu",
    "hong", "moon", "mun", "yang", "bae", "baek", "paek", "ko", "go", "koh",
    "jeon", "jun", "chun", "joo", "ju", "shim", "sim", "na", "min", "ha",
    "koo", "ku", "gu", "heo", "huh", "hur", "woo", "do", "noh", "roh",
    "sohn", "son", "yeom", "yum", "chae", "gong", "kong", "byun", "pyun",
    "byeon", "gil", "ji", "chi", "jang", "chang", "im", "lim", "yun", "yoon",
    "shin", "sin", "oh", "suh", "seo",
}


def _normalize_tokens(s: str) -> set:
    if not s:
        return set()
    s = _NOISE_RE.sub(" ", s)
    parts = re.split(r"[_\-ㅡ/／,\s]+", s)
    tokens = {p.lower() for p in parts if p}
    return tokens - _SURNAME_STOPWORDS


def _strip_round(raw: str):
    body = raw.strip()
    m = _ROUND_RE.search(body)
    if m:
        return body[: m.start()].strip(), int(m.group(1))
    return body, None


def _confirmed_key_map(confirmed_rows) -> dict:
    result = {}
    for row in confirmed_rows or []:
        raw = row.get("카카오표시이름") or ""
        body, _ = _strip_round(raw)
        key = frozenset(_normalize_tokens(body))
        if key:
            result[key] = str(row.get("member_id"))
    return result


def match_submitters(
    kakao_raw_names, dashboard_members: list, confirmed_rows=None, display_name_overrides=None
) -> dict:
    parsed = []
    for raw in kakao_raw_names:
        body, round_num = _strip_round(raw)
        tokens = _normalize_tokens(body)
        if tokens:
            parsed.append({"raw": raw, "round_num": round_num, "tokens": tokens})

    # member_meta는 항상 문자열 id로 키를 맞춘다 (대시보드 API는 int, 구글시트는
    # 문자열로 돌아오기 때문에 여기서 통일하지 않으면 확정매칭 조회가 조용히 실패한다).
    member_meta = {}
    for m in dashboard_members:
        name_tokens = _normalize_tokens(m.get("real_name") or "")
        override = (display_name_overrides or {}).get(str(m.get("id")))
        if override:
            name_tokens = name_tokens | _normalize_tokens(override)
        member_meta[str(m["id"])] = {
            "member": m,
            "nick_tokens": _normalize_tokens(m.get("nickname") or ""),
            "name_tokens": name_tokens,
            "round_count": m.get("round_count"),
        }

    confirmed_map = _confirmed_key_map(confirmed_rows)

    matched_ids = set()
    ambiguous = []

    for item in parsed:
        tokens = item["tokens"]

        confirmed_member_id = confirmed_map.get(frozenset(tokens))
        if confirmed_member_id is not None and confirmed_member_id in member_meta:
            matched_ids.add(confirmed_member_id)
            member_meta[confirmed_member_id]["member"]["_matched_raw"] = item["raw"]
            continue

        candidates = []
        for meta in member_meta.values():
            nick_hit = bool(tokens & meta["nick_tokens"])
            name_hit = bool(tokens & meta["name_tokens"])
            if not (nick_hit or name_hit):
                continue
            score = 2 if (nick_hit and name_hit) else 1
            round_match = bool(
                item["round_num"] is not None
                and meta["round_count"]
                and int(meta["round_count"]) == item["round_num"]
            )
            candidates.append((score, round_match, name_hit, meta["member"], item["raw"]))

        if not candidates:
            continue

        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        top_score, top_round = candidates[0][0], candidates[0][1]
        top = [c for c in candidates if c[0] == top_score and c[1] == top_round]

        if len(top) == 1 and (top[0][0] == 2 or top[0][2]):
            matched_ids.add(str(top[0][3]["id"]))
            top[0][3]["_matched_raw"] = top[0][4]
        elif len(top) == 1:
            ambiguous.append({"raw": item["raw"], "candidates": [top[0][3]]})
        else:
            ambiguous.append({"raw": item["raw"], "candidates": [c[3] for c in top]})

    matched = [meta["member"] for mid, meta in member_meta.items() if mid in matched_ids]
    unmatched_members = [meta["member"] for mid, meta in member_meta.items() if mid not in matched_ids]

    return {"matched": matched, "unmatched_members": unmatched_members, "ambiguous": ambiguous}
