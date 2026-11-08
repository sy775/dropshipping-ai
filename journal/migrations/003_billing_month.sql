-- Mois de facturation : un lot envoyé le 30 et collecté le 2 du mois suivant reste imputé au mois d'envoi,
-- celui où sa réservation a été contrôlée et comptée. Rempli à l'insertion (mois de `ts` par défaut) ;
-- les lignes réelles d'un lot recopient celui de leur ligne de réservation. month_spend filtre sur ce champ.

ALTER TABLE llm_calls ADD COLUMN billing_month TEXT;            -- 'YYYY-MM' (UTC)
UPDATE llm_calls SET billing_month = substr(ts, 1, 7) WHERE billing_month IS NULL;
CREATE INDEX idx_llm_calls_billing ON llm_calls (purpose, billing_month);

DROP VIEW v_llm_cost_monthly;
CREATE VIEW v_llm_cost_monthly AS
SELECT COALESCE(billing_month, substr(ts, 1, 7)) AS month,
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
