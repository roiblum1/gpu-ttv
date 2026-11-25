-- The DC stage and the profile-less collector were removed (ADR-0008): server-scan is the only
-- inventory source. Drops the tables migrations 0003-0005 created in databases that ran them.
DROP TABLE IF EXISTS collector_servers;
DROP TABLE IF EXISTS collector_reads;
DROP TABLE IF EXISTS collector_first_seen;
DROP TABLE IF EXISTS collector_profile_first_seen;
DROP TABLE IF EXISTS collector_managers;
-- Source health rows of the removed collector managers would otherwise stay on the Evidence page.
DELETE FROM source_state WHERE starts_with(name, 'collector:');
