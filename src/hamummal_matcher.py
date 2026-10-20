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


def _normalize_tokens(s: str, protect: frozenset = frozenset()) -> set:
    """
    protect: 성씨 로마자 표기와 우연히 겹치는 조원 닉네임(예: "Min", "Ha")이
    통째로 걸러지는 걸 막기 위해, 이번 매칭 과정에서는 "잡음"으로 치지 않을
    단어들. match_submitters가 조원 명단에서 뽑아서 모든 토큰화 호출에
    똑같이 넘겨준다 — 그래야 원문 쪽과 조원 쪽이 같은 기준으로 비교된다.
    """
    if not s:
        return set()
    s = _NOISE_RE.sub(" ", s)
    parts = re.split(r"[_\-ㅡ/／,\s]+", s)
    tokens = {p.lower() for p in parts if p}
    return tokens - (_SURNAME_STOPWORDS - protect)


def _strip_round(raw: str):
    body = raw.strip()
    m = _ROUND_RE.search(body)
    if m:
        return body[: m.start()].strip(), int(m.group(1))
    return body, None


def _confirmed_key_map(confirmed_rows, protect: frozenset = frozenset()) -> dict:
    result = {}
    for row in confirmed_rows or []:
        raw = row.get("카카오표시이름") or ""
        body, _ = _strip_round(raw)
        key = frozenset(_normalize_tokens(body, protect=protect))
        if key:
            result[key] = str(row.get("member_id"))
    return result


def match_submitters(
    kakao_raw_names, dashboard_members: list, confirmed_rows=None, display_name_overrides=None,
    all_chat_senders=None,
) -> dict:
    # 성씨 로마자 표기 목록(_SURNAME_STOPWORDS)은 원래 "박민수" 같은 원문에서
    # 성(姓) 부분을 잡음으로 걸러내려는 용도인데, 조원 닉네임이 우연히 그
    # 목록과 겹치면(예: 닉네임이 "Min", "Ha") 그 닉네임 전체가 통째로 걸러져서
    # 그 조원만 영영 매칭이 안 되는 문제가 있었다. 그래서 실제로 등록된 조원
    # 닉네임 단어들은 이 매칭 과정 전체에서 성씨 필터링 대상에서 빼준다.
    protected_words = set()
    for m in dashboard_members:
        for part in re.split(r"[_\-ㅡ/／,\s]+", m.get("nickname") or ""):
            if part:
                protected_words.add(part.lower())
    protect = frozenset(protected_words)

    parsed = []
    for raw in kakao_raw_names:
        body, _round_num = _strip_round(raw)
        tokens = _normalize_tokens(body, protect=protect)
        if tokens:
            parsed.append({"raw": raw, "tokens": tokens})

    # member_meta는 항상 문자열 id로 키를 맞춘다 (대시보드 API는 int, 구글시트는
    # 문자열로 돌아오기 때문에 여기서 통일하지 않으면 확정매칭 조회가 조용히 실패한다).
    member_meta = {}
    for m in dashboard_members:
        name_tokens = _normalize_tokens(m.get("real_name") or "", protect=protect)
        override = (display_name_overrides or {}).get(str(m.get("id")))
        if override:
            name_tokens = name_tokens | _normalize_tokens(override, protect=protect)
        member_meta[str(m["id"])] = {
            "member": m,
            "nick_tokens": _normalize_tokens(m.get("nickname") or "", protect=protect),
            "name_tokens": name_tokens,
        }

    confirmed_map = _confirmed_key_map(confirmed_rows, protect=protect)

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
            if nick_hit and not name_hit:
                # 닉네임만 겹치고 실명은 안 겹치는 경우 — raw에 이 사람의
                # 실명이 아닌 다른 토큰(= 다른 사람의 실명일 가능성이 큰 텍스트)
                # 이 더 있으면 닉네임이 같을 뿐인 다른 사람으로 보고 후보에서
                # 아예 뺀다. raw가 정말 닉네임 하나뿐이면(leftover 없음) 실명
                # 정보가 없을 뿐이니 계속 확인필요 후보로 남긴다.
                leftover = tokens - meta["nick_tokens"]
                if leftover:
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

    # "매칭(그 사람이 누구인지 식별)"과 "오늘 제출했는지"는 별개다. 오늘
    # 메시지가 없어서 미제출로 남더라도, 전체 소통방(all_chat_senders, 날짜
    # 상관없이 업로드 파일 전체)에서 한 번이라도 이름/닉네임이 겹치는 원문을
    # 찾은 적 있으면 "식별은 된 사람"이라는 힌트(_known_raw)를 붙여준다 —
    # 화면에서 "(매칭안됨)" 대신 그 원문을 보여주기 위함일 뿐, 제출 여부
    # 판정에는 아무 영향 없다(그건 여전히 위 kakao_raw_names만 쓴다).
    all_tokens_cache = []
    for raw in (all_chat_senders or []):
        body, _ = _strip_round(raw)
        raw_tokens = _normalize_tokens(body, protect=protect)
        if raw_tokens:
            all_tokens_cache.append((raw, raw_tokens))

    # 동명이인(같은 실명 토큰을 가진 조원이 2명 이상) 여부를 미리 파악해둔다.
    # 동명이인인 경우, 실명만 겹치는 원문(raw)은 둘 중 누구 건지 알 수 없으니
    # "식별된 적 있음" 힌트로 함부로 쓰면 안 되고, 반드시 닉네임까지 겹쳐야
    # 그 사람이라고 확신할 수 있다.
    name_token_counts = {}
    for meta in member_meta.values():
        key = frozenset(meta["name_tokens"])
        if key:
            name_token_counts[key] = name_token_counts.get(key, 0) + 1

    unmatched_members = []
    for mid, meta in member_meta.items():
        if mid in matched_ids:
            continue
        member = meta["member"]
        if all_tokens_cache:
            nick_tokens = meta["nick_tokens"]
            name_tokens = meta["name_tokens"]
            has_namesake = name_token_counts.get(frozenset(name_tokens), 0) > 1
            if nick_tokens or name_tokens:
                nick_matched_raw = None
                name_only_fallback_raw = None
                for raw, raw_tokens in all_tokens_cache:
                    nick_hit = bool(raw_tokens & nick_tokens)
                    name_hit = bool(raw_tokens & name_tokens)
                    if not (nick_hit or name_hit):
                        continue
                    if nick_hit and not name_hit:
                        # 위 확인필요 후보 선정과 같은 기준: 닉네임만 겹치고
                        # 실명이 다른(원문에 다른 실명 토큰이 더 있는) 경우는
                        # 같은 사람이 아닐 수 있으니 식별된 걸로 치지 않는다.
                        leftover = raw_tokens - nick_tokens
                        if leftover:
                            continue
                    if nick_hit:
                        # 닉네임까지 겹치는 원문은 그 사람이 거의 확실하니
                        # 바로 채택하고 더 찾지 않는다.
                        nick_matched_raw = raw
                        break
                    # 여기부턴 실명만 겹치고 닉네임은 안 겹치는 경우. 이 조원에게
                    # 등록된 닉네임이 있다면(nick_tokens가 비어있지 않다면), 그
                    # 닉네임과 다른 원문은 다른 사람일 수 있으니(예: 조원 닉네임은
                    # "용진몽"인데 원문은 "Alan") 동명이인 여부와 상관없이 힌트로
                    # 쓰지 않는다 — 실명만 보고 넘겨짚지 않는다. 등록된 닉네임이
                    # 아예 없는 조원이고 동명이인도 없을 때만, 실명 단독 일치를
                    # (가장 먼저 찾은 것으로) 힌트 후보로 남겨둔다.
                    if not nick_tokens and not has_namesake and name_only_fallback_raw is None:
                        name_only_fallback_raw = raw
                resolved_raw = nick_matched_raw or name_only_fallback_raw
                if resolved_raw:
                    member["_known_raw"] = resolved_raw
        unmatched_members.append(member)

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
