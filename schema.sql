-- LN2 scale readings from the Arduino
CREATE TABLE IF NOT EXISTS scale_readings (
    id       BIGSERIAL PRIMARY KEY,
    time     TIMESTAMPTZ NOT NULL,
    weight   DOUBLE PRECISION,
    temp     DOUBLE PRECISION,
    humidity DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_scale_time ON scale_readings (time DESC);
