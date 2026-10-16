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
        body, _round_num = _strip_round(raw)
        tokens = _normalize_tokens(body)
        if tokens:
            parsed.append({"raw": raw, "tokens": tokens})

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
            candidates.append((score, name_hit, meta["member"], item["raw"]))

        if not candidates:
            continue

        candidates.sort(key=lambda c: c[0], reverse=True)
        top_score = candidates[0][0]
        top = [c for c in candidates if c[0] == top_score]

        if len(top) == 1 and (top[0][0] == 2 or top[0][1]):
            matched_ids.add(str(top[0][2]["id"]))
            top[0][2]["_matched_raw"] = top[0][3]
        elif len(top) == 1:
            ambiguous.append({"raw": item["raw"], "candidates": [top[0][2]]})
        else:
            ambiguous.append({"raw": item["raw"], "candidates": [c[2] for c in top]})

    matched = [meta["member"] for mid, meta in member_meta.items() if mid in matched_ids]
    unmatched_members = [meta["member"] for mid, meta in member_meta.items() if mid not in matched_ids]

    return {"matched": matched, "unmatched_members": unmatched_members, "ambiguous": ambiguous}


def find_chat_sender_match(display_name: str, chat_senders) -> str:
    """
    "이름 고치기"에서 고른(또는 직접 입력한) 이름이 특정 날짜의 실제 카카오톡
    발신자 목록(chat_senders) 중 누구에 해당하는지 찾는다.

    그냥 문자열이 똑같은 사람이 있으면 그 사람을 그대로 쓰지만, 회차(예:
    "(7회)")는 매일 올라가서 날짜마다 끝부분이 다르므로, 회차를 뗀 나머지
    이름이 같은 사람이 있으면 "그 날짜의 실제 회차가 붙은 원문"을 찾아 돌려준다
    (그래야 다른 날짜로 그대로 옮겨붙여도 회차가 그 날짜 기준으로 맞게 남는다).
    못 찾으면 None — 즉, 그 이름으로 그 날짜에 실제로 보낸 메시지가 없다는 뜻.
    """
    chat_senders = chat_senders or []
    if display_name in chat_senders:
        return display_name

    body, _ = _strip_round(display_name)
    target_tokens = _normalize_tokens(body)
    if not target_tokens:
        return None

    for raw in chat_senders:
        raw_body, _ = _strip_round(raw)
        if _normalize_tokens(raw_body) == target_tokens:
            return raw
    return None
