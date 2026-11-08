# Procédure mensuelle : rapprochement journal / console Anthropic

Objectif : vérifier une fois par mois que le coût calculé par le journal (`llm_calls`) correspond à la facture
Anthropic. **Écart toléré : 5 %** du montant de la console. Au-delà, on cherche la cause avant de relever un plafond.
Durée : environ 15 minutes, dans les premiers jours du mois M+1, pour le mois M.

## 1. Avant de comparer
1. Attendre que tous les lots du mois M soient collectés :
   `python -m journal.release_reservation list` ne doit rien afficher, et aucune ligne `reserved` ne doit rester
   pour le mois M :
   ```sql
   SELECT call_id, agent, ts, cost_usd FROM llm_calls
   WHERE status = 'reserved' AND COALESCE(billing_month, substr(ts, 1, 7)) = 'AAAA-MM';
   ```
   Une réservation restante est une estimation, pas un coût réel : la traiter d'abord (collecte, `attach` ou `release`).
   **Délai de 29 jours pour `attach`.** Anthropic conserve les résultats d'un lot 29 jours après sa création
   (doc « Batch processing »). Passé ce délai, `collect_batch` ne peut plus lire les résultats : `attach` refuse
   donc toute réservation de plus de `batch.results_retention_days` (29 j, dans `config/agents.yaml`) et renvoie
   vers `release --reason ... --confirm`. Donc :
   - traiter chaque alerte de lot oublié (26 h) dans la semaine, sans attendre le rapprochement mensuel :
     les lots du début du mois M ont déjà plus de 29 jours au moment du rapprochement de M+1 ;
   - au-delà de 29 jours, utiliser `release`, mais **noter d'abord le coût affiché par la console** (s'il existe)
     dans le `--reason` et dans l'historique. `release` met le coût journalisé à 0 : si le lot a été facturé, le
     journal du mois M sous-estime ce montant, qui devient une cause d'écart connue pour ce mois. Les plafonds du
     mois en cours ne sont pas touchés ;
   - si le lot n'existe pas dans la console, `release` est la bonne commande, quel que soit l'âge.
2. Vérifier qu'aucune ligne `pending` ne traîne (elles sont libérées automatiquement après `pending_ttl_minutes`).

## 2. Montant du journal
Sur le serveur (`sqlite3 data/journal.sqlite`), remplacer `AAAA-MM` :
```sql
-- Total réel du mois, hors dry-run, par usage (prod / eval)
SELECT purpose, ROUND(SUM(cost_usd), 4) AS cost_usd, SUM(web_search_requests) AS recherches
FROM llm_calls
WHERE dry_run = 0 AND provider = 'anthropic'
  AND COALESCE(billing_month, substr(ts, 1, 7)) = 'AAAA-MM'
GROUP BY purpose;

-- Détail par modèle et par jour (UTC), pour localiser un écart
SELECT substr(ts, 1, 10) AS jour, model, mode, ROUND(SUM(cost_usd), 4) AS cost_usd,
       SUM(tokens_in) AS tin, SUM(tokens_out) AS tout,
       SUM(tokens_cache_read) AS cr, SUM(tokens_cache_write) AS cw
FROM llm_calls
WHERE dry_run = 0 AND provider = 'anthropic'
  AND COALESCE(billing_month, substr(ts, 1, 7)) = 'AAAA-MM'
GROUP BY jour, model, mode ORDER BY jour;
```

## 2 bis. Calibrage de `search_tokens_estimate` (agent veille)
L'estimation avant appel compte, pour chaque recherche web possible (`web_search_max_uses`, 5 pour veille),
`search_tokens_estimate` tokens d'entrée (4000 au départ) : ce sont les résultats que le modèle relit. Ce chiffre
est une hypothèse. Il se calibre chaque mois sur les appels veille réels, en comparant les tokens d'entrée
estimés aux tokens d'entrée réels.

Valeurs à reporter depuis `config/agents.yaml` : `M` = `web_search_max_uses` de veille (5), `S` =
`search_tokens_estimate` (4000), `1.20` = 1 + `input_margin`. Les tokens réels d'entrée sont
`tokens_in + tokens_cache_read + tokens_cache_write`.

```sql
-- Lots veille du mois : estimation (ligne de réservation, statut released) contre réel (lignes des éléments)
WITH lots AS (
  SELECT r.batch_id,
         r.tokens_in_estimated AS est_in,
         COUNT(i.call_id) AS elements,
         SUM(i.tokens_in + i.tokens_cache_read + i.tokens_cache_write) AS reel_in,
         SUM(i.web_search_requests) AS recherches
  FROM llm_calls r
  JOIN llm_calls i ON i.batch_id = r.batch_id AND i.call_id <> r.call_id
  WHERE r.agent = 'veille' AND r.status = 'released' AND r.dry_run = 0
    AND COALESCE(r.billing_month, substr(r.ts, 1, 7)) = 'AAAA-MM'
  GROUP BY r.batch_id
  HAVING COUNT(i.call_id) = COUNT(i.tokens_in)        -- lots sans élément en échec
),
appels AS (
  SELECT batch_id, est_in, elements, reel_in, recherches FROM lots
  UNION ALL                                            -- appels veille synchrones, s'il y en a
  SELECT call_id, tokens_in_estimated, 1,
         tokens_in + tokens_cache_read + tokens_cache_write, web_search_requests
  FROM llm_calls
  WHERE agent = 'veille' AND mode = 'sync' AND status = 'ok' AND dry_run = 0
    AND COALESCE(billing_month, substr(ts, 1, 7)) = 'AAAA-MM'
)
SELECT batch_id, elements, est_in, reel_in, recherches,
       ROUND(100.0 * (reel_in - est_in) / est_in, 1) AS ecart_pct,
       -- entrée hors recherches, telle que l'adaptateur l'a estimée (sans marge)
       ROUND((est_in - elements * 5 * 4000) / 1.20) AS base_est,
       -- tokens réellement ajoutés par recherche
       CASE WHEN recherches > 0
            THEN ROUND((reel_in - (est_in - elements * 5 * 4000) / 1.20) / recherches) END AS tokens_par_recherche
FROM appels ORDER BY ecart_pct DESC;
```

Lecture :
- **`ecart_pct` > 0 sur une ligne** : le réel a dépassé l'estimation, qui doit pourtant être un majorant. La
  réservation était trop basse et le plafond aurait pu être dépassé. À traiter en priorité : relever `S`
  sans attendre.
- **`tokens_par_recherche`** : prendre la médiane du mois, sur au moins 10 lignes avec recherches. Si elle
  s'écarte de plus de 20 % de `S`, autrement dit si `|médiane − S| / S > 0,20`, remplacer
  `search_tokens_estimate` par la médiane arrondie au multiple de 500 supérieur. Faire un commit et le noter
  dans l'historique.
  - Si l'écart vient d'une baisse, `S` plus élevé n'était que prudent ; l'ajuster libère du plafond.
  - Si l'écart vient d'une hausse, l'estimation était trop basse : l'ajuster est obligatoire.
- Moins de 10 lignes avec recherches : ne rien changer, noter « échantillon insuffisant ».

## 3. Montant de la console
Console Anthropic, page Usage / Cost (https://platform.claude.com), workspace du projet, mois M en UTC :
noter le coût total, puis le détail par modèle et la ligne des recherches web.

## 4. Comparer
`écart = |journal − console| / console`. Noter les deux montants et l'écart dans le tableau en bas de ce fichier.

- **Écart ≤ 5 %** : rien à faire.
- **Écart > 5 %** : chercher la cause avec les points ci-dessous, la noter, puis corriger le tarif (`prices.yaml`,
  avec `verified_on`) ou le code. Si le journal est **sous** la console, il sous-estime la dépense : les plafonds
  ne protègent plus. C'est le cas à traiter en priorité.

## 5. Causes d'écart connues
| Cause | Sens de l'écart | Vérification |
| --- | --- | --- |
| Lot envoyé le dernier jour du mois et collecté le suivant : le journal l'impute au mois d'envoi (`billing_month`), la console peut le dater au traitement | l'un ou l'autre selon le mois | requête par jour, lignes `mode = 'batch'` en début et fin de mois |
| Refus du modèle avant toute sortie, dans une catégorie non facturée : le journal compte le coût de l'usage renvoyé, Anthropic ne le facture pas | journal > console | `SELECT COUNT(*), SUM(cost_usd) FROM llm_calls WHERE stop_reason = 'refusal' ...` |
| Éléments de lot `errored`, `canceled`, `expired` : non facturés, journalisés à 0 | aucun | |
| Tarif modifié chez Anthropic, ou tarif expiré (`valid_until`) | l'un ou l'autre | comparer `prices.yaml` à la page Pricing |
| Recherches web : 10 $ / 1 000, sans remise en lot | journal < console si `web_search_requests` manque | colonne `recherches` de la requête 2 |
| Lot retrouvé après 29 jours puis libéré par `release` : facturé, mais journalisé à 0 | journal < console | `error LIKE 'libération manuelle%'` ; montant noté dans l'historique |
| Appels faits hors adaptateur (console, script de test) avec la même clé | journal < console | une clé d'API dédiée au serveur évite ce cas |
| Routage `inference_geo` US (x 1,1) : non utilisé par le projet | journal < console | aucun paramètre `inference_geo` dans le code |

## Historique
| Mois | Journal ($) | Console ($) | Écart | Tokens / recherche (médiane, S) | Cause / action |
| --- | --- | --- | --- | --- | --- |
| | | | | | |
