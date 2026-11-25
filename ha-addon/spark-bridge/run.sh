#!/command/with-contenv bashio
# Turn the add-on options into the bridge's .env (kept in /data, which survives updates).
ENV_FILE=/data/spark.env
{
  echo "BRIDGE_TRANSPORT=ble"
  echo "BRIDGE_BLE_NAME=$(bashio::config 'node_name')"
  echo "BRIDGE_BLE_ADDRESS=$(bashio::config 'ble_address')"
  echo "BRIDGE_BLE_ADAPTER=$(bashio::config 'ble_adapter')"
  echo "BRIDGE_GUILD_IDS=$(bashio::config 'guild_ids')"
  echo "BRIDGE_DB_PATH=/data/spark.db"
  echo "DISCORD_TOKEN=$(bashio::config 'discord_token')"
} > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"
export SPARK_ENV_FILE="${ENV_FILE}"
bashio::log.info "Starting SpArk Room Bridge"
exec /opt/venv/bin/spark-bridge --log-level "$(bashio::config 'log_level')"
