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
| Appels faits hors adaptateur (console, script de test) avec la même clé | journal < console | une clé d'API dédiée au serveur évite ce cas |
| Routage `inference_geo` US (x 1,1) : non utilisé par le projet | journal < console | aucun paramètre `inference_geo` dans le code |

## Historique
| Mois | Journal ($) | Console ($) | Écart | Cause / action |
| --- | --- | --- | --- | --- |
| | | | | |
