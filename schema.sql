-- Supabase(Postgres) 스키마.
-- 원래 구글시트로 관리하던 5개 탭을 그대로 옮긴 테이블 구조다.
-- Supabase 프로젝트의 SQL Editor에서 이 파일 내용을 그대로 붙여넣고 실행하면
-- (처음 설정할 때는 "Run and enable RLS"로 실행) 동일한 구조를 재현할 수 있다.
-- RLS를 켜도 앱이 쓰는 postgres 롤은 bypassrls라서 앱 동작에는 영향 없다.

CREATE TABLE IF NOT EXISTS members (
    id SERIAL PRIMARY KEY,
    cohort TEXT NOT NULL,             -- 기수
    name TEXT NOT NULL,               -- 이름
    phone TEXT NOT NULL,              -- 휴대폰번호 (숫자만)
    team TEXT,                        -- 조
    role TEXT,                        -- 역할
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 가입일시
    UNIQUE (cohort, phone)
);

CREATE TABLE IF NOT EXISTS cohorts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,        -- 기수명
    start_date DATE,                  -- 시작일
    end_date DATE                     -- 종료일
);

CREATE TABLE IF NOT EXISTS cache_rows (
    team TEXT PRIMARY KEY,                          -- 조
    missing_text TEXT NOT NULL DEFAULT '',          -- 미제출텍스트
    tag_text TEXT NOT NULL DEFAULT '',              -- 태그리스트
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()   -- 갱신시각
);

CREATE TABLE IF NOT EXISTS confirmed_matches (
    id SERIAL PRIMARY KEY,
    team TEXT NOT NULL,                             -- 조
    member_id TEXT NOT NULL,
    nickname TEXT,                                  -- 닉네임
    real_name TEXT,                                 -- 실명
    kakao_display_name TEXT,                        -- 카카오표시이름
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),-- 확정일시
    UNIQUE (team, member_id, kakao_display_name)
);

CREATE TABLE IF NOT EXISTS display_name_overrides (
    team TEXT NOT NULL,                             -- 조
    member_id TEXT NOT NULL,
    display_name TEXT NOT NULL,                     -- 표시이름
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 갱신시각
    PRIMARY KEY (team, member_id)
);
