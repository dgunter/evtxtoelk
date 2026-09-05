# ruff: noqa: E501
import json

import pytest

from evtxtoelk._ecs_tables import ARRAY_FIELDS, FIELD_TYPES
from evtxtoelk.ecs import ECS_VERSION, document_id, ecs_index_body, to_ecs, unflatten
from evtxtoelk.transform import iter_record_xml

NS = 'xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'


def record(
    *,
    provider="Microsoft-Windows-Security-Auditing",
    guid="{54849625-5478-4994-A5BA-3E3B0328C30D}",
    event_id="4624",
    channel="Security",
    computer="WKS01.example.corp",
    keywords="0x8020000000000000",
    level="0",
    opcode="0",
    task="12544",
    version="2",
    record_id="42",
    user_sid="",
    data=None,
    unnamed=(),
    binary=None,
    user_data=None,
):
    items = "".join(f'<Data Name="{k}">{v}</Data>' for k, v in (data or {}).items())
    items += "".join(f"<Data>{v}</Data>" for v in unnamed)
    if binary:
        items += f"<Binary>{binary}</Binary>"
    body = f"<EventData>{items}</EventData>" if (data or unnamed or binary) else ""
    if user_data:
        body += f"<UserData>{user_data}</UserData>"
    security = f'<Security UserID="{user_sid}"/>' if user_sid else "<Security/>"
    return (
        f"<Event {NS}><System>"
        f'<Provider Name="{provider}" Guid="{guid}"/>'
        f"<EventID>{event_id}</EventID><Version>{version}</Version><Level>{level}</Level>"
        f"<Task>{task}</Task><Opcode>{opcode}</Opcode><Keywords>{keywords}</Keywords>"
        f'<TimeCreated SystemTime="2020-07-28 13:22:18.799348+00:00"/>'
        f"<EventRecordID>{record_id}</EventRecordID>"
        f'<Correlation ActivityID="{{ad38ff07-bc05-4620-a79a-51e18f454768}}"/>'
        f'<Execution ProcessID="4" ThreadID="56"/>'
        f"<Channel>{channel}</Channel><Computer>{computer}</Computer>{security}"
        f"</System>{body}</Event>"
    )


def flat(doc, prefix=""):
    out = {}
    for k, v in doc.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flat(v, key + "."))
        else:
            out[key] = v
    return out


# -- generic layer --------------------------------------------------------------------


def test_generic_layer_system_fields():
    f = flat(to_ecs(record(data={"SubjectUserName": "alice"}, user_sid="S-1-5-18")))
    assert f["@timestamp"] == "2020-07-28T13:22:18.799348+00:00"
    assert f["event.created"] == f["@timestamp"] == f["winlog.time_created"]
    assert f["event.kind"] == "event"
    assert f["event.code"] == "4624"
    assert f["event.provider"] == "Microsoft-Windows-Security-Auditing"
    assert f["event.module"] == "security"
    assert f["event.dataset"] == "system.security"
    assert f["ecs.version"] == ECS_VERSION
    assert f["host.name"] == "WKS01.example.corp"
    assert f["log.level"] == "information"
    assert f["winlog.channel"] == "Security"
    assert f["winlog.event_id"] == "4624"
    assert f["winlog.record_id"] == "42"
    assert f["winlog.version"] == 2
    assert f["winlog.opcode"] == "Info"
    assert f["winlog.task"] == "Logon"
    assert f["winlog.keywords"] == ["Audit Success"]
    assert f["winlog.activity_id"] == "{AD38FF07-BC05-4620-A79A-51E18F454768}"
    assert f["winlog.process.pid"] == 4
    assert f["winlog.process.thread.id"] == 56
    assert f["winlog.user.identifier"] == "S-1-5-18"
    assert f["winlog.user.name"] == "SYSTEM"
    assert f["winlog.user.domain"] == "NT AUTHORITY"
    assert f["winlog.user.type"] == "Well Known Group"
    assert f["winlog.event_data.SubjectUserName"] == "alice"
    assert "Event" not in to_ecs(record())
    assert "event.original" not in f


@pytest.mark.parametrize(
    ("level", "name"),
    [
        ("0", "information"),
        ("1", "critical"),
        ("2", "error"),
        ("3", "warning"),
        ("4", "information"),
        ("5", "verbose"),
        ("9", "9"),
    ],
)
def test_log_levels(level, name):
    assert flat(to_ecs(record(level=level)))["log.level"] == name


def test_keywords_decode_known_bits_and_keep_unknown():
    assert flat(to_ecs(record(keywords="0x8010000000000000")))["winlog.keywords"] == [
        "Audit Failure"
    ]
    assert flat(to_ecs(record(keywords="0x80000000000000")))["winlog.keywords"] == ["Classic"]
    assert flat(to_ecs(record(keywords="0x8000000000000010")))["winlog.keywords"] == ["0x10"]
    assert flat(to_ecs(record(keywords="0x8020000000000010")))["winlog.keywords"] == [
        "Audit Success",
        "0x10",
    ]


def test_opcode_names():
    assert flat(to_ecs(record(opcode="1")))["winlog.opcode"] == "Start"
    assert flat(to_ecs(record(opcode="42")))["winlog.opcode"] == "42"


def test_event_data_variants():
    f = flat(
        to_ecs(record(data={"A": "1", "Empty": "", "Dash": "-"}, unnamed=("x", "y"), binary="0A0B"))
    )
    assert f["winlog.event_data.A"] == "1"
    assert f["winlog.event_data.Dash"] == "-"
    assert f["winlog.event_data.param1"] == "x"
    assert f["winlog.event_data.param2"] == "y"
    assert f["winlog.event_data.Binary"] == "0A0B"
    assert f["winlog.event_data.Empty"] == ""  # event_data keeps empty values, as Winlogbeat does


def test_user_data_is_nested_without_xml_noise():
    xml = record(
        provider="Microsoft-Windows-TerminalServices-RemoteConnectionManager",
        channel="Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational",
        event_id="1149",
        user_data='<EventXML xmlns="Event_NS"><Param1>bob</Param1><Param3 Attr="v">10.0.0.5</Param3></EventXML>',
    )
    doc = to_ecs(xml)
    assert doc["winlog"]["user_data"] == {
        "xml_name": "EventXML",
        "Param1": "bob",
        "Param3": {"Attr": "v", "value": "10.0.0.5"},
    }
    assert (
        doc["event"]["dataset"]
        == "windows.microsoft_windows_terminalservices_remoteconnectionmanager_operational"
    )
    assert "module" not in doc["event"]


def test_original_and_meta_and_id():
    xml = record()
    doc = to_ecs(xml, original=True, meta={"observer.name": "lab", "tags": "case-1"})
    assert doc["event"]["original"] == xml
    assert doc["observer"]["name"] == "lab"
    assert doc["tags"] == ["case-1"]
    assert document_id(doc) == document_id(to_ecs(xml))
    assert document_id({"winlog": {"channel": "Security"}}) is None


def test_rejects_non_event_xml():
    with pytest.raises(ValueError):
        to_ecs("<Nope/>")


def test_accepts_parsed_dict():
    import xmltodict

    parsed = xmltodict.parse(record())
    assert to_ecs(parsed)["event"]["code"] == "4624"


# -- security -------------------------------------------------------------------------


def test_security_logon_4624():
    f = flat(
        to_ecs(
            record(
                data={
                    "SubjectUserSid": "S-1-5-18",
                    "SubjectUserName": "WKS01$",
                    "SubjectDomainName": "EXAMPLE",
                    "SubjectLogonId": "0x3e7",
                    "TargetUserSid": "S-1-5-21-1-2-3-1001",
                    "TargetUserName": "alice",
                    "TargetDomainName": "EXAMPLE",
                    "TargetLogonId": "0x1414c8",
                    "LogonType": "10",
                    "WorkstationName": "LAPTOP7",
                    "IpAddress": "::ffff:10.0.0.7",
                    "IpPort": "51234",
                    "ProcessId": "0x704",
                    "ProcessName": "C:\\Windows\\System32\\winlogon.exe",
                    "LmPackageName": "-",
                }
            )
        )
    )
    assert f["event.action"] == "logged-in"
    assert f["event.category"] == ["authentication"]
    assert f["event.type"] == ["start"]
    assert f["event.outcome"] == "success"
    assert f["user.id"] == "S-1-5-18"
    assert f["user.name"] == "WKS01$"
    assert f["user.domain"] == "EXAMPLE"
    assert f["user.target.name"] == "alice"
    assert f["user.target.id"] == "S-1-5-21-1-2-3-1001"
    assert f["user.effective.name"] == "alice"
    assert f["related.user"] == ["WKS01$", "alice"]
    assert f["winlog.logon.type"] == "RemoteInteractive"
    assert f["winlog.logon.id"] == "0x1414c8"
    assert f["source.ip"] == "10.0.0.7"
    assert f["source.port"] == 51234
    assert f["source.domain"] == "LAPTOP7"
    assert f["related.ip"] == ["10.0.0.7"]
    assert f["process.pid"] == 0x704
    assert f["process.executable"] == "C:\\Windows\\System32\\winlogon.exe"
    assert f["process.name"] == "winlogon.exe"
    assert f["winlog.event_data.LmPackageName"] == "-"  # raw value kept


def test_security_failed_logon_4625():
    f = flat(
        to_ecs(
            record(
                event_id="4625",
                keywords="0x8010000000000000",
                data={
                    "TargetUserName": "bob",
                    "TargetDomainName": "EXAMPLE",
                    "LogonType": "3",
                    "Status": "0xc000006d",
                    "SubStatus": "0xc000006a",
                    "FailureReason": "%%2313",
                    "IpAddress": "-",
                    "IpPort": "0",
                },
            )
        )
    )
    assert f["event.outcome"] == "failure"
    assert f["winlog.logon.type"] == "Network"
    assert f["winlog.logon.failure.status"].startswith("This is either due to a bad username")
    assert f["winlog.logon.failure.sub_status"] == "User logon with misspelled or bad password"
    assert f["winlog.logon.failure.reason"] == "Unknown user name or bad password."
    assert "source.ip" not in f
    assert f["source.port"] == 0  # Winlogbeat keeps port 0
    assert "user.effective.name" not in f  # failed logon: no effective user
    assert f["user.target.name"] == "bob"


def test_security_process_creation_4688():
    f = flat(
        to_ecs(
            record(
                event_id="4688",
                data={
                    "SubjectUserSid": "S-1-5-21-1-2-3-500",
                    "SubjectUserName": "admin",
                    "SubjectDomainName": "EXAMPLE",
                    "NewProcessId": "0x1fc",
                    "NewProcessName": "C:\\Windows\\System32\\cmd.exe",
                    "CommandLine": 'cmd.exe /c "dir C:\\Program Files"',
                    "ProcessId": "0x278",
                    "ParentProcessName": "C:\\Windows\\explorer.exe",
                    "TokenElevationType": "%%1936",
                },
            )
        )
    )
    assert f["event.action"] == "created-process"
    assert f["event.category"] == ["process"]
    assert f["process.pid"] == 508
    assert f["process.executable"] == "C:\\Windows\\System32\\cmd.exe"
    assert f["process.name"] == "cmd.exe"
    assert f["process.command_line"] == 'cmd.exe /c "dir C:\\Program Files"'
    assert f["process.args"] == [
        "cmd.exe",
        "/c",
        '"dir',
        "C:\\Program",
        'Files"',
    ]  # plain split, as Winlogbeat
    assert f["process.args_count"] == 5
    assert f["process.parent.pid"] == 632
    assert f["process.parent.executable"] == "C:\\Windows\\explorer.exe"
    assert f["process.parent.name"] == "explorer.exe"


def test_security_group_membership_and_privileges():
    f = flat(
        to_ecs(
            record(
                event_id="4728",
                data={
                    "MemberName": "CN=Bob Smith,CN=Users,DC=example,DC=corp",
                    "MemberSid": "S-1-5-21-1-2-3-1105",
                    "TargetUserName": "Domain Admins",
                    "TargetDomainName": "EXAMPLE",
                    "TargetSid": "S-1-5-21-1-2-3-512",
                    "SubjectUserName": "admin",
                    "SubjectDomainName": "EXAMPLE",
                    "SubjectUserSid": "S-1-5-21-1-2-3-500",
                    "PrivilegeList": "-",
                },
            )
        )
    )
    assert f["group.name"] == "Domain Admins"
    assert f["group.id"] == "S-1-5-21-1-2-3-512"
    assert f["user.target.name"] == "Bob Smith"
    assert f["user.target.id"] == "S-1-5-21-1-2-3-1105"
    assert f["user.name"] == "admin"
    p = flat(
        to_ecs(
            record(
                event_id="4672", data={"PrivilegeList": "SeDebugPrivilege\n\t\t\tSeTcbPrivilege"}
            )
        )
    )
    assert p["winlog.event_data.PrivilegeList"] == ["SeDebugPrivilege", "SeTcbPrivilege"]
    assert p["event.action"] == "logged-in-special"


def test_security_share_and_object_access():
    f = flat(
        to_ecs(
            record(
                event_id="5145",
                data={
                    "ShareName": "\\\\*\\C$",
                    "ShareLocalPath": "\\??\\C:\\",
                    "RelativeTargetName": "Windows\\Temp\\x.txt",
                    "AccessMask": "0x100180",
                    "AccessList": "%%4416 %%4423",
                },
            )
        )
    )
    assert f["file.name"] == "x.txt"
    assert f["file.target_path"] == "\\\\*\\C$\\Windows\\Temp\\x.txt"
    assert f["winlog.event_data.AccessList"] == ["ReadData (or ListDirectory)", "ReadAttributes"]
    assert f["winlog.event_data.AccessMaskDescription"]
    assert f["file.directory"] == "\\??\\C:\\"
    g = flat(
        to_ecs(
            record(
                event_id="4663", data={"ObjectType": "File", "ObjectName": "C:\\secret\\plan.docx"}
            )
        )
    )
    assert g["file.path"] == "C:\\secret\\plan.docx"
    assert g["file.extension"] == "docx"
    assert g["event.category"] == ["file"]
    assert g["event.type"] == ["access"]
    assert g["event.action"] == "object-access-attempted"
    r = flat(
        to_ecs(
            record(
                event_id="4663",
                data={"ObjectType": "Key", "ObjectName": "\\REGISTRY\\MACHINE\\SOFTWARE\\X"},
            )
        )
    )
    assert r["registry.path"] == "\\REGISTRY\\MACHINE\\SOFTWARE\\X"


def test_security_coded_values_and_kerberos():
    f = flat(
        to_ecs(
            record(
                event_id="4768",
                data={
                    "TicketOptions": "0x40810010",
                    "TicketEncryptionType": "0x12",
                    "Status": "0x0",
                    "TargetUserName": "alice",
                    "IpAddress": "::ffff:10.1.1.1",
                },
            )
        )
    )
    assert "Forwardable" in f["winlog.event_data.TicketOptionsDescription"]
    assert f["winlog.event_data.TicketEncryptionTypeDescription"] == "AES256-CTS-HMAC-SHA1-96"
    assert f["winlog.event_data.StatusDescription"] == "KDC_ERR_NONE"
    assert f["source.ip"] == "10.1.1.1"
    u = flat(
        to_ecs(
            record(
                event_id="4720",
                data={"NewUacValue": "0x15", "TargetUserName": "svc", "SubjectUserName": "admin"},
            )
        )
    )
    assert set(u["winlog.event_data.NewUACList"]) == {
        "USER_ACCOUNT_DISABLED",
        "USER_PASSWORD_NOT_REQUIRED",
        "USER_NORMAL_ACCOUNT",
    }
    assert u["event.action"] == "added-user-account"
    s = flat(
        to_ecs(
            record(
                event_id="4697",
                data={
                    "ServiceName": "PSEXESVC",
                    "ServiceType": "0x10",
                    "ServiceFileName": "%SystemRoot%\\PSEXESVC.exe",
                },
            )
        )
    )
    assert s["service.name"] == "PSEXESVC"
    assert s["service.type"] == "Win32 Own Process"
    assert s["process.executable"] == "%SystemRoot%\\PSEXESVC.exe"
    a = flat(
        to_ecs(
            record(
                event_id="4719",
                data={
                    "SubcategoryGuid": "{0CCE9210-69AE-11D9-BED3-505054503030}",
                    "AuditPolicyChanges": "%%8448, %%8450",
                },
            )
        )
    )
    assert a["winlog.event_data.SubCategory"] == "Security State Change"
    assert a["winlog.event_data.Category"] == "System"
    assert a["winlog.event_data.AuditPolicyChanges"] == ["Success removed", "Failure removed"]


# -- sysmon ---------------------------------------------------------------------------


def sysmon(event_id, data):
    return record(
        provider="Microsoft-Windows-Sysmon",
        guid="{5770385F-C22A-43E0-BF4C-06F5698FFBD9}",
        channel="Microsoft-Windows-Sysmon/Operational",
        event_id=event_id,
        keywords="0x8000000000000000",
        level="4",
        task=event_id,
        user_sid="S-1-5-18",
        data=data,
    )


def test_sysmon_process_create():
    f = flat(
        to_ecs(
            sysmon(
                "1",
                {
                    "RuleName": "-",
                    "UtcTime": "2019-04-27 15:57:53.368",
                    "ProcessGuid": "{365abb72-7c01-5cc4-0000-00102b3e0c00}",
                    "ProcessId": "2680",
                    "Image": "C:\\Users\\IEUser\\Downloads\\Flash_update.exe",
                    "FileVersion": "?",
                    "Description": "Flash",
                    "Product": "?",
                    "Company": "Adobe",
                    "OriginalFileName": "flash.exe",
                    "CommandLine": '"C:\\Users\\IEUser\\Downloads\\Flash_update.exe" --quiet',
                    "CurrentDirectory": "C:\\Users\\IEUser\\Downloads\\",
                    "User": "MSEDGEWIN10\\IEUser",
                    "LogonGuid": "{365abb72-7ab1-5cc4-0000-0020bef40000}",
                    "LogonId": "0xf4be",
                    "IntegrityLevel": "High",
                    "Hashes": "SHA1=B4E581F173F782A2F1DA5D29C95946EE500EB2D0,MD5=42893ADBC36605EC79B5BD610759947E,SHA256=1A061C74619DE6AF8C02CBA0FA00754BDD9E3515C0E08CAD6350C7ADFC8CDD5B,IMPHASH=40BEC1A4A3BCB7D3089B5E1532386613",
                    "ParentProcessGuid": "{365abb72-7acc-5cc4-0000-0010b2470300}",
                    "ParentProcessId": "2772",
                    "ParentImage": "C:\\Windows\\explorer.exe",
                    "ParentCommandLine": "C:\\Windows\\Explorer.EXE",
                },
            )
        )
    )
    assert f["event.module"] == "sysmon"
    assert f["event.dataset"] == "windows.sysmon_operational"
    assert f["@timestamp"] == "2019-04-27T15:57:53.368000+00:00"  # Sysmon's UtcTime
    assert f["event.created"] == "2020-07-28T13:22:18.799348+00:00"  # the log write time
    assert f["event.action"] == "Process Create (rule: ProcessCreate)"
    assert f["winlog.task"] == "Process Create (rule: ProcessCreate)"
    assert f["event.category"] == ["process"]
    assert f["event.type"] == ["start"]
    assert f["process.pid"] == 2680
    assert f["process.entity_id"] == "{365ABB72-7C01-5CC4-0000-00102B3E0C00}"
    assert f["process.executable"].endswith("Flash_update.exe")
    assert f["process.name"] == "Flash_update.exe"
    assert f["process.args"] == ["C:\\Users\\IEUser\\Downloads\\Flash_update.exe", "--quiet"]
    assert f["process.args_count"] == 2
    assert f["process.working_directory"] == "C:\\Users\\IEUser\\Downloads\\"
    assert (
        f["process.hash.sha256"]
        == "1a061c74619de6af8c02cba0fa00754bdd9e3515c0e08cad6350c7adfc8cdd5b"
    )
    assert f["process.hash.md5"] == "42893adbc36605ec79b5bd610759947e"
    assert f["process.pe.imphash"] == "40bec1a4a3bcb7d3089b5e1532386613"
    assert f["process.pe.company"] == "Adobe"
    assert f["process.pe.original_file_name"] == "flash.exe"
    assert "process.pe.product" not in f  # "?" is unknown
    assert f["process.parent.pid"] == 2772
    assert f["process.parent.name"] == "explorer.exe"
    assert f["process.parent.entity_id"] == "{365ABB72-7ACC-5CC4-0000-0010B2470300}"
    assert f["user.domain"] == "MSEDGEWIN10"
    assert f["user.name"] == "IEUser"
    assert f["user.id"] == "S-1-5-18"  # from System/Security UserID, as Winlogbeat does
    assert f["related.user"] == ["IEUser"]
    assert len(f["related.hash"]) == 4  # md5, sha1, sha256 and imphash
    assert "rule.name" not in f
    assert f["winlog.event_data.Image"].endswith("Flash_update.exe")  # kept, by design


def test_sysmon_network_connection_and_dns():
    f = flat(
        to_ecs(
            sysmon(
                "3",
                {
                    "RuleName": "Suspicious NetCon",
                    "ProcessId": "3912",
                    "Image": "C:\\ps.exe",
                    "User": "H\\bob",
                    "Protocol": "tcp",
                    "Initiated": "true",
                    "SourceIsIpv6": "false",
                    "SourceIp": "10.0.2.15",
                    "SourceHostname": "H.home",
                    "SourcePort": "49727",
                    "SourcePortName": "-",
                    "DestinationIsIpv6": "false",
                    "DestinationIp": "172.217.17.132",
                    "DestinationHostname": "x.1e100.net",
                    "DestinationPort": "80",
                    "DestinationPortName": "http",
                },
            )
        )
    )
    assert f["event.category"] == ["network"]
    assert f["network.transport"] == "tcp"
    assert f["network.protocol"] == "http"
    assert f["network.direction"] == "egress"
    assert f["network.type"] == "ipv4"
    assert f["network.community_id"] == "1:7kcEPj3gkfHC/5OrZfaWce5iuV4="
    assert f["source.ip"] == "10.0.2.15"
    assert f["source.port"] == 49727
    assert f["destination.domain"] == "x.1e100.net"
    assert f["related.ip"] == ["10.0.2.15", "172.217.17.132"]
    assert f["rule.name"] == "Suspicious NetCon"
    d = flat(
        to_ecs(
            sysmon(
                "22",
                {
                    "ProcessId": "2428",
                    "Image": "C:\\svchost.exe",
                    "QueryName": "www.example.com",
                    "QueryStatus": "0",
                    "QueryResults": "type:  5 www.example.com.edgekey.net;::ffff:93.184.216.34;",
                },
            )
        )
    )
    assert d["network.protocol"] == "dns"
    assert d["dns.question.name"] == "www.example.com"
    assert d["sysmon.dns.status"] == "SUCCESS"
    assert d["dns.resolved_ip"] == ["93.184.216.34"]
    assert d["related.hosts"] == ["www.example.com", "www.example.com.edgekey.net"]
    doc = to_ecs(
        sysmon(
            "22",
            {
                "QueryName": "a",
                "QueryResults": "type:  5 b.net;::ffff:1.2.3.4;",
                "QueryStatus": "9003",
            },
        )
    )
    assert doc["dns"]["answers"] == [
        {"data": "b.net", "type": "CNAME"},
        {"data": "1.2.3.4", "type": "A"},
    ]
    assert doc["sysmon"]["dns"]["status"] == "DNS_ERROR_RCODE_NAME_ERROR"


def test_sysmon_registry_file_and_image_load():
    r = flat(
        to_ecs(
            sysmon(
                "13",
                {
                    "EventType": "SetValue",
                    "TargetObject": "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run\\Updater",
                    "Details": "C:\\evil.exe",
                    "Image": "C:\\reg.exe",
                    "ProcessId": "1",
                },
            )
        )
    )
    assert r["event.category"] == ["configuration", "registry"]
    assert r["registry.hive"] == "HKLM"
    assert r["registry.path"] == "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run\\Updater"
    assert r["registry.key"] == "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run\\Updater"
    assert r["registry.value"] == "Updater"
    assert r["registry.data.strings"] == ["C:\\evil.exe"]
    assert r["registry.data.type"] == "SZ"
    d = flat(
        to_ecs(
            sysmon(
                "13",
                {
                    "EventType": "SetValue",
                    "TargetObject": "HKLM\\SOFTWARE\\X\\Flag",
                    "Details": "DWORD (0x00000004)",
                    "Image": "C:\\reg.exe",
                    "ProcessId": "1",
                },
            )
        )
    )
    assert d["registry.data.type"] == "SZ_DWORD"
    assert d["registry.data.strings"] == ["4"]
    k = flat(
        to_ecs(
            sysmon(
                "12",
                {
                    "EventType": "CreateKey",
                    "TargetObject": "HKU\\S-1-5-21-1\\Software\\Key 1",
                    "Image": "C:\\reg.exe",
                    "ProcessId": "1",
                },
            )
        )
    )
    assert k["registry.key"] == "S-1-5-21-1\\Software\\Key 1"
    assert "registry.value" not in k
    assert r["registry.data.type"] == "SZ"
    d = flat(
        to_ecs(
            sysmon(
                "13",
                {
                    "EventType": "SetValue",
                    "TargetObject": "HKLM\\SOFTWARE\\X\\Flag",
                    "Details": "DWORD (0x00000004)",
                    "Image": "C:\\reg.exe",
                    "ProcessId": "1",
                },
            )
        )
    )
    assert d["registry.data.type"] == "SZ_DWORD"
    assert d["registry.data.strings"] == ["4"]
    k = flat(
        to_ecs(
            sysmon(
                "12",
                {
                    "EventType": "CreateKey",
                    "TargetObject": "HKU\\S-1-5-21-1\\Software\\Key 1",
                    "Image": "C:\\reg.exe",
                    "ProcessId": "1",
                },
            )
        )
    )
    assert k["registry.key"] == "S-1-5-21-1\\Software\\Key 1"
    assert "registry.value" not in k
    f = flat(
        to_ecs(
            sysmon(
                "11",
                {
                    "TargetFilename": "C:\\Users\\bob\\AppData\\Local\\Temp\\drop.dll",
                    "CreationUtcTime": "2020-01-01 00:00:00.000",
                    "Image": "C:\\x.exe",
                    "ProcessId": "7",
                },
            )
        )
    )
    assert f["event.category"] == ["file"]
    assert f["file.path"].endswith("drop.dll")
    assert f["file.name"] == "drop.dll"
    assert f["file.directory"] == "C:\\Users\\bob\\AppData\\Local\\Temp"
    assert f["file.extension"] == "dll"
    i = flat(
        to_ecs(
            sysmon(
                "7",
                {
                    "ImageLoaded": "C:\\Windows\\System32\\ntdll.dll",
                    "Hashes": "SHA256=AB,MD5=CD",
                    "Signed": "true",
                    "Signature": "Microsoft Windows",
                    "SignatureStatus": "Valid",
                    "Company": "Microsoft",
                    "OriginalFileName": "ntdll.dll",
                    "Image": "C:\\x.exe",
                    "ProcessId": "7",
                },
            )
        )
    )
    assert i["file.hash.sha256"] == "ab"
    assert i["file.hash.md5"] == "cd"
    assert i["file.code_signature.signed"] is True
    assert i["file.code_signature.valid"] is True
    assert i["file.code_signature.subject_name"] == "Microsoft Windows"
    assert i["file.pe.company"] == "Microsoft"
    assert i["file.pe.original_file_name"] == "ntdll.dll"
    assert "process.hash.sha256" not in i
    e = flat(to_ecs(sysmon("255", {"ID": "0x2", "Description": "oops"})))
    assert e["error.code"] == "0x2"


# -- powershell -----------------------------------------------------------------------


def test_powershell_script_block_4104():
    f = flat(
        to_ecs(
            record(
                provider="Microsoft-Windows-PowerShell",
                channel="Microsoft-Windows-PowerShell/Operational",
                event_id="4104",
                keywords="0x0",
                level="5",
                task="2",
                data={
                    "MessageNumber": "1",
                    "MessageTotal": "1",
                    "ScriptBlockText": "Get-Process lsass",
                    "ScriptBlockId": "27f08bda-c330-419f-b83b-eb5c0f699930",
                    "Path": "C:\\Users\\Public\\dump.ps1",
                },
            )
        )
    )
    assert f["event.module"] == "powershell"
    assert f["event.dataset"] == "windows.powershell_operational"
    assert f["powershell.file.script_block_text"] == "Get-Process lsass"
    assert f["powershell.file.script_block_id"] == "27f08bda-c330-419f-b83b-eb5c0f699930"
    assert f["powershell.sequence"] == 1
    assert f["powershell.total"] == 1
    assert f["file.name"] == "dump.ps1"
    assert f["file.extension"] == "ps1"


def test_powershell_engine_lifecycle_400():
    blob = "\n".join(
        [
            "NewEngineState=Available",
            "PreviousEngineState=None",
            "SequenceNumber=13",
            "HostName=ConsoleHost",
            "HostVersion=5.1.19041.1",
            "HostId=44b8d66c-f5a2-4abb-ac7d-6db73990a6d3",
            "HostApplication=C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -NoProfile",
            "EngineVersion=5.1.19041.1",
            "RunspaceId=405e3aad-8d1e-4a6c-9d2c-96d4a9eaa2d5",
            "PipelineId=",
            "CommandName=",
            "CommandType=",
            "ScriptName=",
            "CommandPath=",
            "CommandLine=",
        ]
    )
    f = flat(
        to_ecs(
            record(
                provider="PowerShell",
                guid="",
                channel="Windows PowerShell",
                event_id="400",
                keywords="0x80000000000000",
                level="4",
                task="4",
                unnamed=("Available", "None", blob),
            )
        )
    )
    assert f["event.module"] == "powershell"
    assert f["event.dataset"] == "windows.powershell"
    assert f["event.action"] == "Engine Lifecycle"
    assert f["powershell.engine.new_state"] == "Available"
    assert f["powershell.engine.previous_state"] == "None"
    assert f["event.sequence"] == 13
    assert f["process.title"] == "ConsoleHost"
    assert f["process.entity_id"] == "44b8d66c-f5a2-4abb-ac7d-6db73990a6d3"
    assert f["powershell.process.executable_version"] == "5.1.19041.1"
    assert f["powershell.engine.version"] == "5.1.19041.1"
    assert f["powershell.runspace_id"] == "405e3aad-8d1e-4a6c-9d2c-96d4a9eaa2d5"
    assert f["process.command_line"].endswith("powershell.exe -NoProfile")
    assert f["process.args"][-1] == "-NoProfile"
    assert "powershell.pipeline_id" not in f


# -- types and mapping ----------------------------------------------------------------


def test_type_policy():
    f = flat(
        to_ecs(
            record(
                event_id="4688",
                version="abc",
                data={
                    "NewProcessId": "notanumber",
                    "NewProcessName": "C:\\x.exe",
                    "IpAddress": "LOCAL",
                    "IpPort": "0",
                    "SubjectUserName": "-",
                },
            )
        )
    )
    assert "winlog.version" not in f  # uncoercible long is dropped, not emitted wrong
    assert "process.pid" not in f
    assert f["process.executable"] == "C:\\x.exe"
    assert "source.ip" not in f
    assert f["source.port"] == 0
    assert "user.name" not in f
    assert f["winlog.event_data.SubjectUserName"] == "-"  # event_data keeps the raw value
    assert f["winlog.event_data.NewProcessId"] == "notanumber"


def _check_types(f, name):
    checks = {
        "long": int,
        "integer": int,
        "double": float,
        "float": float,
        "boolean": bool,
        "ip": str,
        "date": str,
        "keyword": str,
        "text": str,
        "wildcard": str,
        "match_only_text": str,
    }
    for key, value in f.items():
        assert value is not None, (name, key)
        if key.startswith(("winlog.event_data.", "winlog.user_data.")):
            continue
        ftype = FIELD_TYPES.get(key)
        if ftype in checks:
            values = value if isinstance(value, list) else [value]
            for v in values:
                assert isinstance(v, checks[ftype]), (name, key, ftype, v)
                assert not (checks[ftype] is int and isinstance(v, bool)), (name, key)
        if key in ARRAY_FIELDS:
            assert isinstance(value, list), (name, key)


def test_sample_files_produce_typed_documents(data_dir):
    for name in ("security.evtx", "system.evtx", "issue_38.evtx"):
        n = 0
        for xml in iter_record_xml(str(data_dir / name)):
            doc = to_ecs(xml)
            f = flat(doc)
            _check_types(f, name)
            assert f["event.code"] == f["winlog.event_id"]
            assert f["@timestamp"]
            json.dumps(doc)
            n += 1
        assert n > 0


def test_unflatten():
    assert unflatten({"a.b": 1, "a.c": 2}) == {"a": {"b": 1, "c": 2}}
    assert unflatten({"a": 1, "a.b": 2}) == {"a": {"value": 1, "b": 2}}


def test_index_body():
    body = ecs_index_body()
    assert body["settings"]["index.mapping.total_fields.limit"] >= 5000
    props = body["mappings"]["properties"]
    assert props["@timestamp"] == {"type": "date"}
    assert props["source"]["properties"]["ip"] == {"type": "ip"}
    assert props["process"]["properties"]["pid"] == {"type": "long"}
    assert props["winlog"]["properties"]["event_id"] == {"type": "keyword"}
    assert props["winlog"]["properties"]["event_data"]["type"] == "object"
    assert props["user"]["properties"]["name"] == {
        "type": "keyword",
        "fields": {"text": {"type": "match_only_text"}},
    }
    assert (
        props["powershell"]["properties"]["command"]["properties"]["invocation_details"]["type"]
        == "object"
    )
    assert props["powershell"]["properties"]["sequence"] == {"type": "long"}
    templates = ecs_index_body()["mappings"]["dynamic_templates"]
    assert templates[0]["winlog_data_as_keyword"]["mapping"]["type"] == "keyword"

    def walk(node, prefix=""):
        for name, spec in node.items():
            assert "type" in spec, prefix + name
            if "properties" in spec:
                assert spec["type"] in ("object", "nested"), prefix + name
                walk(spec["properties"], prefix + name + ".")

    walk(props)


def test_security_filtering_platform_5156():
    f = flat(
        to_ecs(
            record(
                event_id="5156",
                task="12810",
                data={
                    "ProcessID": "1234",
                    "Application": "\\device\\harddiskvolume2\\windows\\system32\\svchost.exe",
                    "Direction": "%%14593",
                    "SourceAddress": "10.0.0.5",
                    "SourcePort": "49152",
                    "DestAddress": "93.184.216.34",
                    "DestPort": "443",
                    "Protocol": "6",
                    "FilterRTID": "68910",
                    "LayerName": "%%14611",
                    "LayerRTID": "48",
                },
            )
        )
    )
    assert f["event.action"] == "connection-permitted"
    assert f["winlog.task"] == "Filtering Platform Connection"
    assert f["source.ip"] == "10.0.0.5"
    assert f["source.port"] == 49152
    assert f["destination.ip"] == "93.184.216.34"
    assert f["destination.port"] == 443
    assert f["network.transport"] == "tcp"
    assert f["network.iana_number"] == "6"
    assert f["network.direction"] == "outbound"
    assert f["winlog.event_data.Direction"] == "Outbound"
    assert f["winlog.event_data.LayerName"] == "Connect"
    assert f["process.pid"] == 1234
    assert f["process.name"] == "svchost.exe"
    assert f["rule.id"] == "68910"
    assert f["related.ip"] == ["10.0.0.5", "93.184.216.34"]


def test_security_registry_value_modified_4657():
    f = flat(
        to_ecs(
            record(
                event_id="4657",
                task="12801",
                data={
                    "SubjectUserName": "admin",
                    "ObjectName": "\\REGISTRY\\MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run",
                    "ObjectValueName": "Updater",
                    "OperationType": "%%1905",
                    "OldValueType": "%%1873",
                    "OldValue": "",
                    "NewValueType": "%%1873",
                    "NewValue": "C:\\evil.exe",
                    "ProcessId": "0x4d0",
                    "ProcessName": "C:\\Windows\\regedit.exe",
                },
            )
        )
    )
    assert f["event.action"] == "registry-value-modified"
    assert f["registry.path"].endswith("CurrentVersion\\Run")
    assert f["registry.value"] == "Updater"
    assert f["registry.data.strings"] == ["C:\\evil.exe"]
    assert f["registry.data.type"] == "REG_SZ"
    assert f["winlog.event_data.OperationType"] == "Existing registry value modified"
    assert f["process.name"] == "regedit.exe"
