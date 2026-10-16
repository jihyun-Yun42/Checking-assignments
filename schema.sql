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
    end_date DATE,                    -- 종료일
    hamummal_review_visible BOOLEAN NOT NULL DEFAULT false,  -- 하멈말 결과확인 공개 여부 (관리자가 수동 on/off)
    hamummal_apply_enabled BOOLEAN NOT NULL DEFAULT false,   -- "소리튠 조장페이지에 반영하기" 버튼 허용 여부 (관리자가 수동 on/off)
    hamummal_start_date DATE          -- 하멈말 날짜별 진행시간 설정 목록의 시작일 (관리자가 수동 설정)
);

-- 이미 cohorts 테이블이 있는 기존 DB에는 위 CREATE TABLE이 적용되지 않으므로
-- (CREATE TABLE IF NOT EXISTS는 기존 테이블에 컬럼을 추가해주지 않음) 아래 ALTER들을
-- Supabase SQL Editor에서 한 번 실행해줘야 한다.
ALTER TABLE cohorts ADD COLUMN IF NOT EXISTS hamummal_review_visible BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cohorts ADD COLUMN IF NOT EXISTS hamummal_apply_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cohorts ADD COLUMN IF NOT EXISTS hamummal_start_date DATE;
ALTER TABLE hamummal_reviews ADD COLUMN IF NOT EXISTS chat_senders JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE hamummal_reviews ADD COLUMN IF NOT EXISTS all_chat_senders JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 날짜별로 다르게 진행되는 하멈말 진행 시간 (관리자가 수동 설정, 나중에 수정 가능)
CREATE TABLE IF NOT EXISTS hamummal_date_windows (
    cohort TEXT NOT NULL,                -- 기수명
    check_date DATE NOT NULL,            -- 하멈말 진행 날짜
    start_time TEXT NOT NULL,            -- 진행 시작 시각 ("HH:MM")
    end_time TEXT NOT NULL,               -- 진행 종료 시각 ("HH:MM")
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cohort, check_date)
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

CREATE TABLE IF NOT EXISTS hamummal_reviews (
    id TEXT PRIMARY KEY,
    check_date DATE NOT NULL,               -- 점검 기준일
    window_desc TEXT,                       -- 점검 시간대 설명 (예: "09:00~10:00")
    review JSONB NOT NULL,                  -- 조별 매칭 결과 전체
    submitter_count INTEGER NOT NULL DEFAULT 0,
    applied_teams JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 조별 "반영 완료" 기록
    chat_senders JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 이 날짜·시간대에 실제로 제출 메시지를 보낸 카카오톡 발신자 이름 목록 (매칭 판정용 — 날짜별로 다름)
    all_chat_senders JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 업로드한 파일 전체에서 날짜/시간 상관없이 제출 메시지를 보낸 적 있는 모든 발신자 이름 목록 ("이름 고치기" 검색용 — 매칭 판정에는 안 씀)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
