from evtxtoelk.community_id import community_id


def test_known_vector_and_direction_independence():
    # From the Community ID spec test data (tcp, 128.232.110.120:34855 -> 66.35.250.204:80)
    expected = "1:LQU9qZlK+B5F3KDmev6m5PMibrg="
    assert community_id("128.232.110.120", "66.35.250.204", "tcp", 34855, 80) == expected
    assert community_id("66.35.250.204", "128.232.110.120", "tcp", 80, 34855) == expected
    assert community_id("128.232.110.120", "66.35.250.204", 6, 34855, 80) == expected


def test_icmp_pairing_ipv6_and_one_way():
    echo = community_id("10.0.0.1", "10.0.0.2", "icmp", 8, 0)
    reply = community_id("10.0.0.2", "10.0.0.1", "icmp", 0, 0)
    assert echo == reply
    assert echo.startswith("1:")
    assert community_id("10.0.0.1", "10.0.0.2", "icmp", 3, 1) != echo  # one-way type
    assert community_id("10.0.0.1", "10.0.0.2", "icmp", None, None).startswith("1:")
    assert community_id("fe80::1", "fe80::2", "icmp", 128, 0) == community_id(
        "fe80::2", "fe80::1", "icmp", 129, 0
    )
    assert community_id("fe80::1", "fe80::2", "icmpv6", 128, 0) == community_id(
        "fe80::1", "fe80::2", "icmp", 128, 0
    )
    assert community_id("fe80::1", "fe80::2", "udp", 1, 2).startswith("1:")
    assert community_id("fe80::1", "fe80::2", 47).startswith("1:")  # GRE: no ports
    assert community_id("10.0.0.1", "10.0.0.1", "tcp", 2, 1) == community_id(
        "10.0.0.1", "10.0.0.1", "tcp", 1, 2
    )


def test_invalid_inputs():
    assert community_id("10.0.0.1", "fe80::2", "tcp", 1, 2) is None
    assert community_id("nope", "10.0.0.2", "tcp", 1, 2) is None
    assert community_id("10.0.0.1", "10.0.0.2", "tcp", None, 2) is None
    assert community_id("10.0.0.1", "10.0.0.2", "bogus", 1, 2) is None
