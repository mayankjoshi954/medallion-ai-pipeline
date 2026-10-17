-- Layer separation = Postgres schemas. `lineage` holds run/agent/alert metadata, not business data.
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS lineage;
