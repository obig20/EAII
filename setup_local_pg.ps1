# setup_local_pg.ps1
$ErrorActionPreference = "Stop"

$pgDir = "c:\Users\Noha\Downloads\pgsql"
$dataDir = "$pgDir\data_test"
$binDir = "$pgDir\bin"

Write-Host "Checking for postgres binaries in $binDir..."
if (-not (Test-Path "$binDir\initdb.exe")) {
    Write-Error "Postgres binaries not found at $binDir"
    exit 1
}

$env:Path = "$binDir;" + $env:Path

if (-not (Test-Path $dataDir)) {
    Write-Host "Initializing PostgreSQL cluster in $dataDir..."
    & "$binDir\initdb.exe" -D $dataDir -U postgres -A trust --no-locale --encoding=UTF8
}

Write-Host "Starting PostgreSQL on port 5432..."
& "$binDir\pg_ctl.exe" -D $dataDir -l "$pgDir\pg.log" start

Start-Sleep -Seconds 2

Write-Host "Creating app user and dailyclose_test database..."
& "$binDir\psql.exe" -U postgres -p 5432 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN CREATE ROLE app WITH LOGIN PASSWORD 'changeme'; END IF; END \$\$;"
& "$binDir\psql.exe" -U postgres -p 5432 -c "DROP DATABASE IF EXISTS dailyclose_test;"
& "$binDir\psql.exe" -U postgres -p 5432 -c "CREATE DATABASE dailyclose_test OWNER postgres;"
& "$binDir\psql.exe" -U postgres -p 5432 -d dailyclose_test -c "GRANT ALL ON SCHEMA public TO app; GRANT ALL ON SCHEMA public TO postgres;"

Write-Host "PostgreSQL is ready!"
