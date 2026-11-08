-- Statuts documentés de llm_calls (la 001 n'est pas modifiée : elle reste rejouable telle quelle).
-- Ajout : `abandoned` = réservation `pending` plus vieille que reservations.pending_ttl_minutes,
-- libérée (cost_usd = 0) à l'ouverture d'une transaction de réservation. Cas typique : processus tué
-- entre la réservation et l'écriture du résultat.
-- Table de référence, non contraignante : elle documente les valeurs et sert aux requêtes de contrôle.

CREATE TABLE llm_call_statuses (
    status      TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT INTO llm_call_statuses (status, description) VALUES
    ('pending',      'appel synchrone en cours, coût estimé réservé'),
    ('ok',           'appel terminé, coût réel'),
    ('error',        'échec de l''appel (cost_usd = 0)'),
    ('format_error', 'réponse hors schéma, coût réel facturé'),
    ('refused',      'refusé par un garde-fou (cost_usd = 0)'),
    ('reserved',     'lot envoyé, coût maximal réservé'),
    ('released',     'réservation de lot remplacée par les coûts réels (cost_usd = 0)'),
    ('abandoned',    'réservation pending expirée et libérée (cost_usd = 0)');

-- Recherche des réservations orphelines à chaque transaction de réservation.
CREATE INDEX idx_llm_calls_status ON llm_calls (status, ts);
