-- A server whose bound profile could not be determined (e.g. an OME TargetName shared by several
-- devices). The engine treats it as an identity conflict, never as "no profile" (ADR-0004).
ALTER TABLE collector_servers ADD COLUMN IF NOT EXISTS profile_unresolved text;
