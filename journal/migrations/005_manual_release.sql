-- Outil journal/release_reservation : une réservation de lot à identifiant provisoire peut être libérée
-- à la main (statut abandoned, cost_usd = 0, date et motif dans error), après vérification chez le fournisseur.

UPDATE llm_call_statuses
   SET description = 'réservation libérée (cost_usd = 0) : pending expirée, ou lot à identifiant provisoire libéré à la main (journal.release_reservation, motif dans error)'
 WHERE status = 'abandoned';
