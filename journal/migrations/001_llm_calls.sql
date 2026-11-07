-- Journal des appels LLM : métriques uniquement, jamais de prompt ni de donnée client.
-- status : pending (appel en cours, coût estimé réservé), ok, error, format_error, refused,
--          reserved (lot envoyé, coût maximal réservé), released (réservation remplacée par le réel).
-- Le cumul mensuel = SUM(cost_usd) des lignes non simulées (dry_run = 0).

CREATE TABLE llm_calls (
    call_id             TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,              -- ISO 8601 UTC
    shop_id             TEXT,
    agent               TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('sync', 'batch')),
    purpose             TEXT NOT NULL CHECK (purpose IN ('prod', 'eval')),
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    tokens_cache_read   INTEGER,
    tokens_cache_write  INTEGER,
    tokens_in_estimated INTEGER,
    tokens_out_estimated INTEGER,
    cost_estimated_usd  REAL,
    cost_usd            REAL NOT NULL DEFAULT 0,
    latency_ms          INTEGER,
    status              TEXT NOT NULL,
    error               TEXT,
    attempt             INTEGER NOT NULL DEFAULT 1,
    batch_id            TEXT,
    dry_run             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_llm_calls_month ON llm_calls (purpose, ts);
CREATE INDEX idx_llm_calls_shop ON llm_calls (shop_id, ts);
CREATE INDEX idx_llm_calls_batch ON llm_calls (batch_id);

-- Déduplication des alertes (ex. alerte à 80 % : une fois par mois et par plafond).
CREATE TABLE llm_alerts (
    alert_key TEXT PRIMARY KEY,
    ts        TEXT NOT NULL
);

CREATE VIEW v_llm_cost_monthly AS
SELECT substr(ts, 1, 7)          AS month,
       purpose,
       agent,
       shop_id,
       dry_run,
       COUNT(*)                  AS n_calls,
       SUM(status = 'ok')        AS n_ok,
       SUM(cost_usd)             AS cost_usd,
       SUM(tokens_in)            AS tokens_in,
       SUM(tokens_out)           AS tokens_out,
       SUM(tokens_cache_read)    AS tokens_cache_read,
       SUM(tokens_cache_write)   AS tokens_cache_write
FROM llm_calls
GROUP BY month, purpose, agent, shop_id, dry_run;
