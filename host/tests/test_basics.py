import pytest

from spark_bridge import config as cfgmod
from spark_bridge.store import ChannelConfig, Store
from spark_bridge.textlimits import clean, public_budget, split_author, tag, truncate_utf8


def test_truncate_on_char_boundary():
    s = "é" * 100  # 2 bytes each
    out, cut = truncate_utf8(s, 151)
    assert cut and len(out.encode()) == 150 and out == "é" * 75
    assert truncate_utf8("abc", 3) == ("abc", False)
    emoji = "a" + "\U0001f600" * 5  # 4 bytes each
    out, cut = truncate_utf8(emoji, 4)
    assert out == "a" and cut
    assert truncate_utf8("x", 0) == ("", True)


def test_budget_and_split():
    assert public_budget("SparkNode") == 160 - 11
    assert public_budget("Spärk") == 160 - 8
    assert split_author("Bob: hi: there") == ("Bob", "hi: there")
    assert split_author("no author here") == ("mesh", "no author here")
    assert tag("robin", "hi", True) == "[robin] hi" and tag("robin", "hi", False) == "hi"
    assert clean("a\n b\t c") == "a b c"


def test_store_channels_roundtrip(tmp_path):
    db = tmp_path / "s.db"
    st = Store(db)
    c = st.update_channel(5, 9, send_target="public", prefix=False, user_id=1)
    st.close()
    st = Store(db)
    assert st.get_channel(5) == c and c.sends_to("public") and not c.sends_to("room")
    both = st.update_channel(5, 9, send_target="both", receive_source="room")
    assert both.sends_to("room") and both.receives("room") and not both.receives("public")
    assert len(st.all_channels()) == 1


def test_seen_names_sent_and_cap():
    st = Store()
    assert not st.seen("r", 1, "aa", "h")
    st.mark_seen("r", 1, "aa", "h")
    assert st.seen("r", 1, "aa", "h") and not st.seen("c", 1, "aa", "h")
    for i in range(600):
        st.mark_seen("r", 1000 + i, "aa", "x")
    assert st.db.execute("SELECT COUNT(*) FROM seen_posts").fetchone()[0] == 500
    st.set_name("DEADBEEF", "Bob")
    assert st.get_name("deadbeef") == "Bob"
    st.add_sent("abc", 1000)
    assert st.was_sent("abc") and not st.was_sent("zzz")
    assert st.get_meta("k") is None
    st.set_meta("k", "v")
    assert st.get_meta("k") == "v"


def test_config_parse_and_errors(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('allow_dangerous_cli = true\n[node]\ntransport="tcp"\ntcp_port=5001\n'
                 '[discord]\nallowed_guild_ids=[1,2]\n[health]\ndawn="06:30"\n[state]\ndb_path="x.db"\n')
    (tmp_path / ".env").write_text("DISCORD_TOKEN=abc\n")
    import os
    os.environ.pop("DISCORD_TOKEN", None)
    c = cfgmod.load(p)
    os.environ.pop("DISCORD_TOKEN", None)
    assert c.node.tcp_port == 5001 and c.discord.allowed_guild_ids == [1, 2]
    assert c.health.dawn.hour == 6 and c.health.dawn.minute == 30 and c.allow_dangerous_cli
    assert c.discord_token == "abc" and c.db_path == str(tmp_path.resolve() / "x.db")
    for bad in ('[node]\ntransport="usb"', '[node]\nblepin="1"', '[health]\ntz="Mars/Base"',
                '[node]\ntransport="serial"', 'bogus=1', '[health]\ndawn="7am"'):
        with pytest.raises(cfgmod.ConfigError):
            cfgmod.parse(__import__("tomllib").loads(bad))
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.load(tmp_path / "missing.toml")


def test_example_env_loads():
    """The committed .env.example (repository root) is a valid bridge configuration."""
    from pathlib import Path
    example = Path(__file__).resolve().parents[2] / ".env.example"
    c = cfgmod.load_env(example, environ={})
    assert c.node.transport == "ble" and c.node.ble_name == "SparkNode"
    assert c.discord.allowed_guild_ids == [] and c.discord_token == ""
    assert c.limits.default_per_minute == 3 and c.health.tz == "UTC"
    assert c.db_path == str(example.parent / "spark-bridge.db")


def test_env_config(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nSPARK_NODE_NAME=Roof\nBRIDGE_TRANSPORT=tcp\nBRIDGE_TCP_PORT=5001\n"
                 "BRIDGE_GUILD_IDS=1, 2\nBRIDGE_HEALTH_DAWN=06:30\nBRIDGE_DB_PATH=x.db\n"
                 "export BRIDGE_ALLOW_DANGEROUS_CLI=1\nDISCORD_TOKEN='abc'\nBRIDGE_BLE_ADDRESS=\n")
    c = cfgmod.load_env(p, environ={})
    assert c.node.tcp_port == 5001 and c.discord.allowed_guild_ids == [1, 2]
    assert c.node.ble_name == "Roof"   # follows the node name unless BRIDGE_BLE_NAME is set
    assert c.health.dawn.hour == 6 and c.health.dawn.minute == 30 and c.allow_dangerous_cli
    assert c.discord_token == "abc" and c.db_path == str(tmp_path.resolve() / "x.db")
    # the process environment wins over the file
    c = cfgmod.load_env(p, environ={"DISCORD_TOKEN": "env", "BRIDGE_TCP_PORT": "6000"})
    assert c.discord_token == "env" and c.node.tcp_port == 6000
    for bad in ("BRIDGE_TRANSPORT=usb", "BRIDGE_TCP_PORT=x", "BRIDGE_TZ=Mars/Base",
                "BRIDGE_TRANSPORT=serial", "BRIDGE_HEALTH_DAWN=7am", "BRIDGE_ALLOW_DANGEROUS_CLI=maybe",
                "BRIDGE_GUILD_IDS=abc"):
        (tmp_path / "bad.env").write_text(bad + "\n")
        with pytest.raises(cfgmod.ConfigError):
            cfgmod.load_env(tmp_path / "bad.env", environ={})
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.load_env(tmp_path / "missing.env", environ={})
