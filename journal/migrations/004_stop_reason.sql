-- Exactitude de la complétion et du coût (session 2, point 1).
-- stop_reason : motif d'arrêt renvoyé par le fournisseur. Seuls end_turn et stop_sequence sont un succès.
-- web_search_requests : recherches web facturées (10 $ / 1 000 chez Anthropic), en plus des tokens.

ALTER TABLE llm_calls ADD COLUMN stop_reason TEXT;
ALTER TABLE llm_calls ADD COLUMN web_search_requests INTEGER;

INSERT OR IGNORE INTO llm_call_statuses (status, description) VALUES
    ('incomplete', 'réponse tronquée ou interrompue (max_tokens, model_context_window_exceeded, pause_turn) : coût réel journalisé, aucun contenu renvoyé');

-- `refused` existait (garde-fou) : il couvre désormais aussi le refus du modèle (stop_reason = refusal).
UPDATE llm_call_statuses
   SET description = 'refusé par un garde-fou (cost_usd = 0, stop_reason NULL) ou par le modèle (stop_reason = refusal, coût réel journalisé, aucun contenu renvoyé)'
 WHERE status = 'refused';
