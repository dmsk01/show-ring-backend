from app.utils.net import ip_subnet


def test_ipv4_collapses_to_slash_24():
    assert ip_subnet("10.20.30.1") == ip_subnet("10.20.30.254") == "10.20.30.0/24"


def test_ipv6_collapses_to_slash_64():
    # Провайдер выдаёт абоненту целую /64 — адреса внутри неё один «клиент».
    assert ip_subnet("2001:db8:1:2::1") == ip_subnet("2001:db8:1:2:ffff::9")
    assert ip_subnet("2001:db8:1:2::1") == "2001:db8:1:2::/64"


def test_garbage_is_returned_as_is():
    # request.client может отсутствовать ("unknown") — не падаем.
    assert ip_subnet("unknown") == "unknown"
