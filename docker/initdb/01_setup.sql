-- This init script runs as the postgres superuser when the container first starts.
-- It creates the postgres superuser password and grants required privileges.

-- Give the 'app' user CREATEDB so Alembic can create the test DB if needed
-- (production: remove CREATEDB, only keep CONNECT)
ALTER USER app CREATEDB;

-- Create a postgres superuser for running migrations with FORCE RLS bypass
-- The password is set via POSTGRES_SUPERUSER_PASSWORD env or defaults below
-- In docker-compose we use the built-in postgres user; just ensure it has a password.
ALTER USER postgres PASSWORD 'changeme';
