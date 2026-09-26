-- One Postgres server, two isolated databases with separate owners.
CREATE USER store WITH PASSWORD 'store';
CREATE DATABASE store OWNER store;
CREATE USER lineage WITH PASSWORD 'lineage';
CREATE DATABASE lineage OWNER lineage;
