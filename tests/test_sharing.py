"""Sharing recommendations: private Tailscale option + Google-auth tip."""
from fbbackup.export_static import TUNNELS, GOOGLE_AUTH_TIP, recommend_sharing


def test_private_tailscale_serve_option_exists():
    t = TUNNELS["tailscale_serve"]
    assert t["public"] is False           # tailnet-only, not a public link
    assert "serve" in t["cmd"]            # `tailscale serve`, not `funnel`
    assert TUNNELS["cloudflared"]["public"] is True
    assert TUNNELS["tailscale"]["public"] is True  # the Funnel one stays public


def test_local_recommendation_offers_private_access():
    rec = recommend_sharing(posts=10000, size_mb=3000, media=5000)
    assert rec["mode"] == "local"
    assert "tailscale serve" in rec["private"]          # private reach-your-own-devices
    assert "tailscale_serve" in rec["tunnels"]          # available in the list
    assert rec["google_tip"] is GOOGLE_AUTH_TIP


def test_google_tip_surfaced_on_every_signup_path():
    for posts in (10000, 3000, 500):                    # local / either / publish
        assert recommend_sharing(posts, 500.0, 100).get("google_tip")
    assert "Google" in GOOGLE_AUTH_TIP
