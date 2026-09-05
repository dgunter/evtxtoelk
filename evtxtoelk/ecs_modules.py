"""Per-module ECS rules for the Security, Sysmon and PowerShell channels.

These reproduce the behaviour of the Winlogbeat module ingest pipelines that
the published field references leave undefined: which ``event.category``,
``event.type`` and ``event.action`` an event id gets, how coded values are
named, and which ``event_data`` values populate ``user.*``, ``process.*``,
``file.*``, ``registry.*``, ``network.*`` and ``dns.*``. The lookup tables
live in :mod:`evtxtoelk._ecs_tables` (``PIPELINE_TABLES``), extracted from
the pinned Beats tag by ``scripts/build_ecs_tables.py``.

Unlike Winlogbeat's Sysmon pipeline, promoted values are copied and left in
``winlog.event_data`` as well, by design (see docs/design-ecs.md).
"""

from __future__ import annotations

import ipaddress
import ntpath
import re
from typing import Any

from evtxtoelk._ecs_tables import PIPELINE_TABLES

Flat = dict[str, Any]

EVENT_DATA = "winlog.event_data"
USER_DATA = "winlog.user_data"
EVENT_CODE = "event.code"
EVENT_OUTCOME = "event.outcome"
WINLOG_TASK = "winlog.task"
RELATED_USER = "related.user"
RELATED_IP = "related.ip"
RELATED_HOSTS = "related.hosts"
USER_NAME = "user.name"
SOURCE_IP = "source.ip"
SOURCE_PORT = "source.port"
SOURCE_DOMAIN = "source.domain"
DESTINATION_IP = "destination.ip"
DESTINATION_PORT = "destination.port"
NETWORK_TRANSPORT = "network.transport"
FILE_PATH = "file.path"
FILE_NAME = "file.name"
REGISTRY_PATH = "registry.path"
REGISTRY_DATA_TYPE = "registry.data.type"
REGISTRY_DATA_STRINGS = "registry.data.strings"

_SEC = PIPELINE_TABLES.get("security", {})
_SYS = PIPELINE_TABLES.get("sysmon", {})
#: Windows Filtering Platform events are not categorised by Winlogbeat; these follow
#: the ECS allowed values the way its firewall integrations do.
_WFP_CATEGORIES: dict[str, dict[str, Any]] = {
    "5150": {
        "category": ["network"],
        "type": ["connection", "denied"],
        "action": "packet-blocked-by-filter-driver",
    },
    "5151": {
        "category": ["network"],
        "type": ["connection", "denied"],
        "action": "packet-blocked-by-filter-driver",
    },
    "5152": {"category": ["network"], "type": ["connection", "denied"], "action": "packet-dropped"},
    "5153": {
        "category": ["network"],
        "type": ["connection", "denied"],
        "action": "packet-blocked-by-filter",
    },
    "5154": {
        "category": ["network"],
        "type": ["connection", "allowed"],
        "action": "listen-permitted",
    },
    "5155": {"category": ["network"], "type": ["connection", "denied"], "action": "listen-blocked"},
    "5156": {
        "category": ["network"],
        "type": ["connection", "allowed"],
        "action": "connection-permitted",
    },
    "5157": {
        "category": ["network"],
        "type": ["connection", "denied"],
        "action": "connection-blocked",
    },
    "5158": {
        "category": ["network"],
        "type": ["connection", "allowed"],
        "action": "bind-permitted",
    },
    "5159": {"category": ["network"], "type": ["connection", "denied"], "action": "bind-blocked"},
}
_SEC_EVENTS = {**_WFP_CATEGORIES, **_SEC.get("events", {})}
_OBJECT_CATEGORIES = {"File": "file", "Key": "registry"}
_OBJECT_ACTIONS = {
    "4656": "object-handle-requested",
    "4658": "object-handle-closed",
    "4660": "object-deleted",
    "4663": "object-access-attempted",
}
_SYS_EVENTS = _SYS.get("events", {})
_LOGON_TYPES = _SEC.get("LogonType", {})
_UAC_FLAGS = _SEC.get("NewUacValue", {})
_TICKET_OPTIONS = _SEC.get("TicketOptions", {})
_TICKET_ENC = _SEC.get("TicketEncryptionType", {})
_KRB_STATUS = _SEC.get("Status", {})
_SERVICE_TYPES = _SEC.get("ServiceName", {})
_SUBCATEGORIES = _SEC.get("SubcategoryGuid", {})
_DESCRIPTIONS = _SEC.get("FailureReason", {}).get("descriptions", {})
_ACCESS_MASKS = _SEC.get("FailureReason", {}).get("AccessMaskDescriptions", {})
_LOGON_FAILURE = _SEC.get("Status_", {})
_TDO_TYPE = _SEC.get("TdoType", {})
_TDO_DIRECTION = _SEC.get("TdoDirection", {})
_TDO_ATTRIBUTES = _SEC.get("TdoAttributes", {})
_DNS_TYPES = _SYS.get("QueryResults", {})
_DNS_STATUS = _SYS.get("table_90", {})
_HIVES = _SYS.get("TargetObject", {})

#: Security event ids whose Target* fields describe a group rather than a user.
_GROUP_EVENTS = {
    "4727", "4728", "4729", "4730", "4731", "4732", "4733", "4734", "4735", "4737",
    "4744", "4745", "4746", "4747", "4748", "4749", "4750", "4751", "4752", "4753",
    "4754", "4755", "4756", "4757", "4758", "4759", "4760", "4761", "4762", "4763",
    "4764", "4799",
}  # fmt: skip
#: Security event ids where Target* is the account that was logged in / acted on.
_LOGON_EVENTS = {"4624", "4625", "4634", "4647", "4648", "4778", "4779", "4964"}
_COMPUTER_EVENTS = {"4741", "4742", "4743"}
_KERBEROS_EVENTS = {"4768", "4769", "4770", "4771", "4772", "4773"}
_OBJECT_EVENTS = {"4656", "4658", "4660", "4663", "4670", "4907"}
_SHARE_EVENTS = {"5140", "5145"}
_SERVICE_EVENTS = {"4697", "7045"}
_NULL = {"", "-", "NULL SID", "%%1793"}

#: Task names as the Sysmon manifest renders them. Winlogbeat has them from the
#: manifest at collection time and uses them for event.action; offline they are
#: fixed per event id.
_SYSMON_TASKS = {
    "1": "Process Create (rule: ProcessCreate)",
    "2": "File creation time changed (rule: FileCreateTime)",
    "3": "Network connection detected (rule: NetworkConnect)",
    "4": "Sysmon service state changed",
    "5": "Process terminated (rule: ProcessTerminate)",
    "6": "Driver loaded (rule: DriverLoad)",
    "7": "Image loaded (rule: ImageLoad)",
    "8": "CreateRemoteThread detected (rule: CreateRemoteThread)",
    "9": "RawAccessRead detected (rule: RawAccessRead)",
    "10": "Process accessed (rule: ProcessAccess)",
    "11": "File created (rule: FileCreate)",
    "12": "Registry object added or deleted (rule: RegistryEvent)",
    "13": "Registry value set (rule: RegistryEvent)",
    "14": "Registry object renamed (rule: RegistryEvent)",
    "15": "File stream created (rule: FileCreateStreamHash)",
    "16": "Sysmon config state changed",
    "17": "Pipe Created (rule: PipeEvent)",
    "18": "Pipe Connected (rule: PipeEvent)",
    "19": "WmiEventFilter activity detected (rule: WmiEvent)",
    "20": "WmiEventConsumer activity detected (rule: WmiEvent)",
    "21": "WmiEventConsumerToFilter activity detected (rule: WmiEvent)",
    "22": "Dns query (rule: DnsQuery)",
    "23": "File Delete archived (rule: FileDelete)",
    "24": "Clipboard changed (rule: ClipboardChange)",
    "25": "Process Tampering (rule: ProcessTampering)",
    "26": "File Delete logged (rule: FileDeleteDetected)",
    "27": "File Block Executable (rule: FileBlockExecutable)",
    "28": "File Block Shredding (rule: FileBlockShredding)",
    "29": "File Executable Detected (rule: FileExecutableDetected)",
    "255": "Error report",
}
#: Security audit task categories (the Task field of Security events).
_SECURITY_TASKS = {
    "12288": "Security State Change",
    "12289": "Security System Extension",
    "12290": "System Integrity",
    "12291": "IPsec Driver",
    "12292": "Other System Events",
    "12544": "Logon",
    "12545": "Logoff",
    "12546": "Account Lockout",
    "12547": "IPsec Main Mode",
    "12548": "Special Logon",
    "12549": "IPsec Quick Mode",
    "12550": "IPsec Extended Mode",
    "12551": "Other Logon/Logoff Events",
    "12552": "Network Policy Server",
    "12553": "User / Device Claims",
    "12554": "Group Membership",
    "12800": "File System",
    "12801": "Registry",
    "12802": "Kernel Object",
    "12803": "SAM",
    "12804": "Other Object Access Events",
    "12805": "Certification Services",
    "12806": "Application Generated",
    "12807": "Handle Manipulation",
    "12808": "File Share",
    "12809": "Filtering Platform Packet Drop",
    "12810": "Filtering Platform Connection",
    "12811": "Detailed File Share",
    "12812": "Removable Storage",
    "12813": "Central Policy Staging",
    "13056": "Sensitive Privilege Use",
    "13057": "Non Sensitive Privilege Use",
    "13058": "Other Privilege Use Events",
    "13312": "Process Creation",
    "13313": "Process Termination",
    "13314": "DPAPI Activity",
    "13315": "RPC Events",
    "13316": "Plug and Play Events",
    "13317": "Token Right Adjusted Events",
    "13568": "Audit Policy Change",
    "13569": "Authentication Policy Change",
    "13570": "Authorization Policy Change",
    "13571": "MPSSVC Rule-Level Policy Change",
    "13572": "Filtering Platform Policy Change",
    "13573": "Other Policy Change Events",
    "13824": "User Account Management",
    "13825": "Computer Account Management",
    "13826": "Security Group Management",
    "13827": "Distribution Group Management",
    "13828": "Application Group Management",
    "13829": "Other Account Management Events",
    "14080": "Directory Service Access",
    "14081": "Directory Service Changes",
    "14082": "Directory Service Replication",
    "14083": "Detailed Directory Service Replication",
    "14336": "Credential Validation",
    "14337": "Kerberos Service Ticket Operations",
    "14338": "Other Account Logon Events",
    "14339": "Kerberos Authentication Service",
}


# -- helpers ------------------------------------------------------------------------------


def _ed(flat: Flat, name: str) -> str | None:
    value = flat.get(f"{EVENT_DATA}.{name}")
    if value is None:
        value = flat.get(f"{USER_DATA}.{name}")
    if value is None or not isinstance(value, str):
        return None
    return None if value.strip() in _NULL else value


def _set(flat: Flat, key: str, value: Any) -> None:
    if value is not None and value != "" and key not in flat:
        flat[key] = value


def _append(flat: Flat, key: str, value: Any) -> None:
    if value is None or value == "":
        return
    current = flat.get(key)
    if current is None:
        flat[key] = [value]
    elif isinstance(current, list):
        if value not in current:
            current.append(value)
    elif current != value:
        flat[key] = [current, value]


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    except ValueError:
        return None


def _hexkey(value: str | None) -> str | None:
    number = _int(value)
    return None if number is None else f"0x{number:x}"


def _ip(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text.lower().startswith("::ffff:") and text.count(".") == 3:
        text = text[7:]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _basename(path: str | None) -> str | None:
    if not path:
        return None
    name = ntpath.basename(path.replace("/", "\\"))
    return name or None


def _flags(value: str | None, table: dict[str, str]) -> list[str] | None:
    """Decode a hex bitmask against a ``{"0x0001": "NAME"}`` table."""
    mask = _int(value)
    if mask is None or not table:
        return None
    names = [name for code, name in table.items() if _int(code) and mask & _int(code)]
    return names or None


def _describe(value: str | None) -> str | None:
    """``%%2313`` style parameter references -> text, when the pipeline knows them."""
    if value is None:
        return None
    text = value.strip()
    return _DESCRIPTIONS.get(text[2:] if text.startswith("%%") else text, value)


def _describe_all(flat: Flat) -> None:
    """Resolve ``%%NNNN`` message references in every event_data value, in place."""
    for key, value in list(flat.items()):
        if not key.startswith(f"{EVENT_DATA}.") or not isinstance(value, str) or "%%" not in value:
            continue
        tokens = [t for t in re.split(r"[\s,]+", value.strip()) if t]
        if not tokens or not all(t.startswith("%%") for t in tokens):
            continue
        names = [_DESCRIPTIONS.get(t[2:], t) for t in tokens]
        flat[key] = names[0] if len(names) == 1 else names


def _split_args(command_line: str) -> list[str]:
    """Windows command line -> argv, honouring double quotes."""
    args, current, quoted = [], [], False
    for ch in command_line:
        if ch == '"':
            quoted = not quoted
        elif ch.isspace() and not quoted:
            if current:
                args.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        args.append("".join(current))
    return args


def _process(
    flat: Flat,
    prefix: str,
    *,
    pid: str | None,
    executable: str | None,
    command_line: str | None = None,
    entity_id: str | None = None,
    quote_aware: bool = False,
) -> None:
    _set(flat, f"{prefix}.pid", _int(pid))
    _set(flat, f"{prefix}.executable", executable)
    _set(flat, f"{prefix}.name", _basename(executable))
    _set(flat, f"{prefix}.entity_id", entity_id)
    if command_line:
        _set(flat, f"{prefix}.command_line", command_line)
        args = _split_args(command_line) if quote_aware else command_line.split()
        if args:
            _set(flat, f"{prefix}.args", args)
            _set(flat, f"{prefix}.args_count", len(args))


def _user(
    flat: Flat, prefix: str, *, sid: str | None, name: str | None, domain: str | None
) -> None:
    _set(flat, f"{prefix}.id", sid)
    _set(flat, f"{prefix}.name", name)
    _set(flat, f"{prefix}.domain", domain)
    _append(flat, RELATED_USER, name)


def _file_from_path(flat: Flat, path: str | None) -> None:
    if not path:
        return
    _set(flat, FILE_PATH, path)
    name = _basename(path)
    _set(flat, FILE_NAME, name)
    directory = ntpath.dirname(path.replace("/", "\\"))
    if directory:
        _set(flat, "file.directory", directory)
    if name and "." in name and not name.endswith("."):
        _set(flat, "file.extension", name.rsplit(".", 1)[1].lower())


def _apply_event_table(flat: Flat, table: dict[str, dict], code: str | None) -> None:
    entry = table.get(code or "")
    if not entry:
        return
    for key in ("category", "type", "action"):
        value = entry.get(key)
        if value is not None:
            flat[f"event.{key}"] = list(value) if isinstance(value, list) else value


# -- Security -----------------------------------------------------------------------------


def _security_task_and_outcome(flat: Flat, code: str | None) -> None:
    task = flat.get(WINLOG_TASK)
    if code == "1102":
        flat[WINLOG_TASK] = "Log clear"
    elif task in _SECURITY_TASKS:
        flat[WINLOG_TASK] = _SECURITY_TASKS[task]
    keywords = flat.get("winlog.keywords") or []
    if "Audit Success" in keywords:
        flat[EVENT_OUTCOME] = "success"
    elif "Audit Failure" in keywords:
        flat[EVENT_OUTCOME] = "failure"


def _security_logon_codes(flat: Flat, code: str | None) -> None:
    logon_type = _ed(flat, "LogonType")
    if logon_type and logon_type in _LOGON_TYPES:
        flat["winlog.logon.type"] = _LOGON_TYPES[logon_type]
    target_logon = _ed(flat, "TargetLogonId")
    if target_logon in (None, "0x0"):
        target_logon = _ed(flat, "SubjectLogonId") or target_logon
    _set(flat, "winlog.logon.id", target_logon)
    if code not in ("4625", "4776"):
        return
    status = _hexkey(_ed(flat, "Status"))
    if status and status in _LOGON_FAILURE:
        flat["winlog.logon.failure.status"] = _LOGON_FAILURE[status]
    sub = _hexkey(_ed(flat, "SubStatus"))
    if sub and sub in _LOGON_FAILURE:
        flat["winlog.logon.failure.sub_status"] = _LOGON_FAILURE[sub]
    reason = _ed(flat, "FailureReason")
    if reason:
        flat["winlog.logon.failure.reason"] = _describe(reason)


def _security_flags(flat: Flat, code: str | None) -> None:
    """Bitmask and enumeration values -> their names, alongside the raw values."""
    for field, target in (("NewUacValue", "NewUACList"), ("OldUacValue", "OldUACList")):
        names = _flags(_ed(flat, field), _UAC_FLAGS)
        if names:
            flat[f"{EVENT_DATA}.{target}"] = names
    names = _flags(_ed(flat, "TicketOptions"), _TICKET_OPTIONS)
    if names:
        flat[f"{EVENT_DATA}.TicketOptionsDescription"] = names
    enc = _hexkey(_ed(flat, "TicketEncryptionType"))
    if enc and enc in _TICKET_ENC:
        flat[f"{EVENT_DATA}.TicketEncryptionTypeDescription"] = _TICKET_ENC[enc]
    status = _hexkey(_ed(flat, "Status"))
    if status and code in _KERBEROS_EVENTS and status in _KRB_STATUS:
        flat[f"{EVENT_DATA}.StatusDescription"] = _KRB_STATUS[status]
    mask = _int(_ed(flat, "AccessMask"))
    if mask:
        names = [
            name for code_, name in _ACCESS_MASKS.items() if _int(code_) and mask & _int(code_)
        ]
        if names:
            flat[f"{EVENT_DATA}.AccessMaskDescription"] = names


def _security_policy_values(flat: Flat) -> None:
    """Audit subcategories, %% message references, privilege lists and trust attributes."""
    guid = _ed(flat, "SubcategoryGuid")
    entry = _SUBCATEGORIES.get(guid.strip("{}").upper()) if guid else None
    if isinstance(entry, list):
        flat[f"{EVENT_DATA}.SubCategory"] = entry[0]
        if len(entry) > 1:
            flat[f"{EVENT_DATA}.Category"] = entry[1]
    elif entry:
        flat[f"{EVENT_DATA}.SubCategory"] = entry
    _describe_all(flat)
    privileges = flat.get(f"{EVENT_DATA}.PrivilegeList")
    if isinstance(privileges, str):
        flat[f"{EVENT_DATA}.PrivilegeList"] = [p for p in re.split(r"[\s,]+", privileges) if p]
    for field, table, target in (
        ("TdoType", _TDO_TYPE, "winlog.trustType"),
        ("TdoDirection", _TDO_DIRECTION, "winlog.trustDirection"),
        ("TdoAttributes", _TDO_ATTRIBUTES, "winlog.trustAttribute"),
    ):
        value = _ed(flat, field)
        if value and value in table:
            flat[target] = table[value]


def _security_coded_values(flat: Flat, code: str | None) -> None:
    """Coded values -> names. Raw values stay in event_data, as in Winlogbeat."""
    _security_flags(flat, code)
    _security_policy_values(flat)


def _security_target_group(
    flat: Flat, sid: str | None, name: str | None, domain: str | None
) -> None:
    for prefix in ("group", "user.target.group"):
        _set(flat, f"{prefix}.id", sid)
        _set(flat, f"{prefix}.name", name)
        _set(flat, f"{prefix}.domain", domain)
    member = _ed(flat, "MemberName")
    if not member:
        return
    parts = [p.strip() for p in member.split(",")]
    cn = next((p[3:] for p in parts if p.upper().startswith("CN=")), member)
    dcs = [p[3:] for p in parts if p.upper().startswith("DC=")]
    _user(flat, "user.target", sid=_ed(flat, "MemberSid"), name=cn, domain=dcs[-1] if dcs else None)


def _security_target(flat: Flat, code: str | None) -> None:
    sid = _ed(flat, "TargetUserSid") or _ed(flat, "TargetSid")
    name, domain = _ed(flat, "TargetUserName"), _ed(flat, "TargetDomainName")
    if code in _KERBEROS_EVENTS or code == "4776":
        # Kerberos and NTLM validation events describe the account being authenticated.
        if name and "@" in name:
            name, _, realm = name.partition("@")
            domain = domain or realm
        _user(flat, "user", sid=sid, name=name, domain=domain)
    elif code in _COMPUTER_EVENTS:
        _set(flat, "winlog.computerObject.id", sid)
        _set(flat, "winlog.computerObject.name", name)
        _set(flat, "winlog.computerObject.domain", domain)
    elif code in _GROUP_EVENTS:
        _security_target_group(flat, sid, name, domain)
    elif name or sid:
        _user(flat, "user.target", sid=sid, name=name, domain=domain)
        logged_in = code in _LOGON_EVENTS and flat.get(EVENT_OUTCOME) != "failure"
        if logged_in or code == "4688":
            _user(flat, "user.effective", sid=sid, name=name, domain=domain)


def _security_accounts(flat: Flat, code: str | None) -> None:
    _user(
        flat,
        "user",
        sid=_ed(flat, "SubjectUserSid"),
        name=_ed(flat, "SubjectUserName"),
        domain=_ed(flat, "SubjectDomainName"),
    )
    _security_target(flat, code)
    if code == "4781":
        _set(flat, "user.changes.name", _ed(flat, "NewTargetUserName"))
        _append(flat, RELATED_USER, _ed(flat, "NewTargetUserName"))
    if _ed(flat, "AccountName"):
        _user(
            flat, "user", sid=None, name=_ed(flat, "AccountName"), domain=_ed(flat, "AccountDomain")
        )
        _set(flat, "winlog.logon.id", _ed(flat, "LogonID"))
        _set(flat, SOURCE_DOMAIN, _ed(flat, "ClientName"))
        _set(flat, SOURCE_IP, _ip(_ed(flat, "ClientAddress")))
    _set(flat, SOURCE_IP, _ip(_ed(flat, "IpAddress")))
    _set(flat, SOURCE_PORT, _int(_ed(flat, "IpPort")))
    _set(flat, SOURCE_DOMAIN, _ed(flat, "WorkstationName"))
    _append(flat, RELATED_IP, flat.get(SOURCE_IP))


def _security_processes(flat: Flat, code: str | None) -> None:
    if code == "4688":
        _process(
            flat,
            "process",
            pid=_ed(flat, "NewProcessId"),
            executable=_ed(flat, "NewProcessName"),
            command_line=_ed(flat, "CommandLine"),
        )
        _process(
            flat,
            "process.parent",
            pid=_ed(flat, "ProcessId"),
            executable=_ed(flat, "ParentProcessName"),
        )
        return
    _process(flat, "process", pid=_ed(flat, "ProcessId"), executable=_ed(flat, "ProcessName"))
    if code == "4689":
        _set(flat, "process.exit_code", _int(_ed(flat, "Status")))


def _security_services(flat: Flat, code: str | None) -> None:
    if code in _SERVICE_EVENTS:
        _set(flat, "service.name", _ed(flat, "ServiceName"))
        service_type = _hexkey(_ed(flat, "ServiceType"))
        if service_type and service_type in _SERVICE_TYPES:
            _set(flat, "service.type", _SERVICE_TYPES[service_type])
        _set(flat, "process.executable", _ed(flat, "ServiceFileName"))
    elif code in _KERBEROS_EVENTS:
        _set(flat, "service.name", _ed(flat, "ServiceName"))


def _security_share(flat: Flat) -> None:
    share, relative = _ed(flat, "ShareName"), _ed(flat, "RelativeTargetName")
    _set(flat, FILE_NAME, _basename(relative) or relative)
    directory = _ed(flat, "ShareLocalPath")
    _set(flat, "file.directory", directory)
    if directory and flat.get(FILE_NAME):
        _set(flat, FILE_PATH, f"{directory.rstrip(chr(92))}\\{flat[FILE_NAME]}")
    if share and relative:
        _set(flat, "file.target_path", f"{share}\\{relative}")
    elif share:
        _set(flat, "file.target_path", share)


def _security_objects(flat: Flat, code: str | None) -> None:
    _security_services(flat, code)
    if code in _SHARE_EVENTS:
        _security_share(flat)
    elif code in _OBJECT_EVENTS:
        object_type = _ed(flat, "ObjectType")
        if object_type == "File":
            _file_from_path(flat, _ed(flat, "ObjectName"))
        elif object_type == "Key":
            _set(flat, REGISTRY_PATH, _ed(flat, "ObjectName"))
        if code not in _SEC.get("events", {}) and object_type in _OBJECT_CATEGORIES:
            # Object access events are not categorised by Winlogbeat; categorise by object type.
            flat.setdefault("event.category", [_OBJECT_CATEGORIES[object_type]])
            flat.setdefault("event.type", ["access"])
            flat.setdefault("event.action", _OBJECT_ACTIONS.get(code, "object-access"))
    elif code == "4657":
        # Registry value modified: not mapped by Winlogbeat, but a direct fit for registry.*
        _set(flat, REGISTRY_PATH, _ed(flat, "ObjectName"))
        _set(flat, "registry.value", _ed(flat, "ObjectValueName"))
        new_value = _ed(flat, "NewValue")
        if new_value is not None:
            flat[REGISTRY_DATA_STRINGS] = [new_value]
        _set(flat, REGISTRY_DATA_TYPE, _ed(flat, "NewValueType"))


#: Windows Filtering Platform events carry a full 5-tuple; Winlogbeat leaves
#: them in event_data, but they are a direct fit for ECS network fields.
_WFP_EVENTS = {"5150", "5151", "5152", "5153", "5154", "5155", "5156", "5157", "5158", "5159"}
_IP_PROTOCOLS = {
    1: "icmp",
    2: "igmp",
    6: "tcp",
    17: "udp",
    41: "ipv6",
    47: "gre",
    58: "icmpv6",
    132: "sctp",
}


def _security_wfp(flat: Flat, code: str | None) -> None:
    if code not in _WFP_EVENTS:
        return
    _set(flat, SOURCE_IP, _ip(_ed(flat, "SourceAddress")))
    _set(flat, SOURCE_PORT, _int(_ed(flat, "SourcePort")))
    _set(flat, DESTINATION_IP, _ip(_ed(flat, "DestAddress")))
    _set(flat, DESTINATION_PORT, _int(_ed(flat, "DestPort")))
    protocol = _int(_ed(flat, "Protocol"))
    if protocol is not None:
        _set(flat, NETWORK_TRANSPORT, _IP_PROTOCOLS.get(protocol, str(protocol)))
        _set(flat, "network.iana_number", str(protocol))
    direction = flat.get(f"{EVENT_DATA}.Direction")
    if isinstance(direction, str) and direction.lower() in ("inbound", "outbound"):
        flat["network.direction"] = direction.lower()
    application = _ed(flat, "Application")
    if application:
        _set(flat, "process.executable", application)
        _set(flat, "process.name", _basename(application))
    _set(flat, "process.pid", _int(_ed(flat, "ProcessID")))
    if flat.get("winlog.event_data.FilterRTID"):
        _set(flat, "rule.id", flat["winlog.event_data.FilterRTID"])
    for key in (SOURCE_IP, DESTINATION_IP):
        _append(flat, RELATED_IP, flat.get(key))


def security(flat: Flat) -> None:
    code = flat.get(EVENT_CODE)
    _apply_event_table(flat, _SEC_EVENTS, code)
    _security_task_and_outcome(flat, code)
    _security_logon_codes(flat, code)
    _security_coded_values(flat, code)
    _security_accounts(flat, code)
    _security_processes(flat, code)
    _security_objects(flat, code)
    _security_wfp(flat, code)


# -- Sysmon -------------------------------------------------------------------------------


def _hashes(flat: Flat, prefix: str, raw: str | None) -> None:
    if not raw:
        return
    for part in raw.split(","):
        algo, sep, digest = part.partition("=")
        if not sep or not digest:
            continue
        algo = algo.strip().lower()
        if set(digest.strip()) == {"0"}:
            continue  # Sysmon logs an all-zero hash when it could not compute one
        if algo == "imphash":
            _set(flat, f"{prefix}.pe.imphash", digest.lower())
        else:
            _set(flat, f"{prefix}.hash.{algo}", digest.lower())
        _append(flat, "related.hash", digest.lower())


def _pe(flat: Flat, prefix: str) -> None:
    for source, target in (
        ("Company", "company"),
        ("Description", "description"),
        ("FileVersion", "file_version"),
        ("Product", "product"),
        ("OriginalFileName", "original_file_name"),
    ):
        value = _ed(flat, source)
        if value and value != "?":
            _set(flat, f"{prefix}.pe.{target}", value)


def _registry(flat: Flat, target: str | None, *, split_value: bool) -> None:
    if not target:
        return
    path = target
    hive_name, _, rest = path.partition("\\")
    hive = _HIVES.get(hive_name.upper())
    if hive:
        path = f"{hive}\\{rest}" if rest else hive
    _set(flat, REGISTRY_PATH, path)
    _set(flat, "registry.hive", hive or hive_name)
    if not rest:
        return
    _set(flat, "registry.key", rest)
    if split_value:
        _, _, value = rest.rpartition("\\")
        _set(flat, "registry.value", value or None)


def _registry_data(flat: Flat, details: str | None) -> None:
    if not details or not flat.get(REGISTRY_PATH):
        return
    kind, sep, rest = details.partition(" ")
    if kind in ("DWORD", "QWORD") and sep and rest.startswith("(") and rest.endswith(")"):
        numbers = [_int(part) for part in rest[1:-1].split("-")]
        if all(n is not None for n in numbers):
            value = numbers[0]
            for low in numbers[1:]:  # QWORD is logged as (high-low)
                value = (value << 32) | low
            flat[REGISTRY_DATA_TYPE] = f"SZ_{kind}"
            flat[REGISTRY_DATA_STRINGS] = [str(value)]
            return
    flat[REGISTRY_DATA_TYPE] = "REG_BINARY" if details == "Binary Data" else "SZ"
    flat[REGISTRY_DATA_STRINGS] = [details]


def _dns_answer(item: str) -> tuple[dict[str, Any] | None, str | None]:
    """One ``QueryResults`` item -> (answer, resolved ip). Fragments give (None, None)."""
    rtype, data = None, item
    if item.startswith("type:"):
        number, _, data = item[5:].strip().partition(" ")
        rtype, data = _DNS_TYPES.get(number, number), data.strip()
    ip = _ip(data)
    if ip:
        return {"data": ip, "type": rtype or ("AAAA" if ":" in ip else "A")}, ip
    if rtype is None:
        return None, None  # neither a typed record nor an address: a truncated fragment
    return {"data": data, "type": rtype}, None


def _dns_answers(flat: Flat, results: str | None) -> None:
    if not results:
        return
    answers, resolved = [], []
    for raw in results.split(";"):
        answer, ip = _dns_answer(raw.strip())
        if answer is None:
            continue
        answers.append(answer)
        if ip:
            resolved.append(ip)
            _append(flat, RELATED_IP, ip)
        else:
            _append(flat, RELATED_HOSTS, str(answer["data"]).rstrip("."))
    if answers:
        flat["dns.answers"] = answers
    if resolved:
        flat["dns.resolved_ip"] = resolved


def _sysmon_process(flat: Flat, code: str | None) -> None:
    hashes = _ed(flat, "Hashes") or _ed(flat, "Hash")
    is_file_event = code in ("6", "7", "15", "29")
    _hashes(flat, "file" if is_file_event else "process", hashes)
    _process(
        flat,
        "process",
        pid=_ed(flat, "ProcessId") or _ed(flat, "SourceProcessId"),
        executable=_ed(flat, "Image") or _ed(flat, "SourceImage") or _ed(flat, "Destination"),
        command_line=_ed(flat, "CommandLine"),
        quote_aware=True,
        entity_id=_ed(flat, "ProcessGuid")
        or _ed(flat, "SourceProcessGuid")
        or _ed(flat, "SourceProcessGUID"),
    )
    _set(flat, "process.thread.id", _int(_ed(flat, "SourceThreadId")))
    _set(flat, "process.working_directory", _ed(flat, "CurrentDirectory"))
    _process(
        flat,
        "process.parent",
        pid=_ed(flat, "ParentProcessId"),
        executable=_ed(flat, "ParentImage"),
        command_line=_ed(flat, "ParentCommandLine"),
        quote_aware=True,
        entity_id=_ed(flat, "ParentProcessGuid"),
    )
    _pe(flat, "file" if code == "7" else "process")


def _sysmon_files(flat: Flat) -> None:
    target = _ed(flat, "TargetFilename") or _ed(flat, "Device") or _ed(flat, "ImageLoaded")
    _file_from_path(flat, target)
    _set(flat, FILE_NAME, _ed(flat, "PipeName"))
    signed = _ed(flat, "Signed")
    if signed is None:
        return
    flat["file.code_signature.signed"] = signed.lower() == "true"
    _set(flat, "file.code_signature.subject_name", _ed(flat, "Signature"))
    status = _ed(flat, "SignatureStatus")
    _set(flat, "file.code_signature.status", status)
    if status is not None:
        flat["file.code_signature.valid"] = status == "Valid"


def _sysmon_dns(flat: Flat) -> None:
    flat["network.protocol"] = "dns"
    _set(flat, "dns.question.name", _ed(flat, "QueryName"))
    _append(flat, RELATED_HOSTS, _ed(flat, "QueryName"))
    _dns_answers(flat, _ed(flat, "QueryResults"))
    status = _ed(flat, "QueryStatus")
    if status is not None:
        flat["sysmon.dns.status"] = _DNS_STATUS.get(status, status)


def _sysmon_flow(flat: Flat) -> None:
    initiated = _ed(flat, "Initiated")
    if initiated is not None:
        flat["network.direction"] = "egress" if initiated.lower() == "true" else "ingress"
    v6 = _ed(flat, "SourceIsIpv6")
    if v6 is not None:
        flat["network.type"] = "ipv6" if v6.lower() == "true" else "ipv4"
    src, dst = flat.get(SOURCE_IP), flat.get(DESTINATION_IP)
    proto = flat.get(NETWORK_TRANSPORT)
    if src and dst and proto:
        from evtxtoelk.community_id import community_id

        cid = community_id(src, dst, proto, flat.get(SOURCE_PORT), flat.get(DESTINATION_PORT))
        if cid:
            flat["network.community_id"] = cid
    _append(flat, RELATED_IP, src)
    _append(flat, RELATED_IP, dst)


def _sysmon_network(flat: Flat, code: str | None) -> None:
    _set(flat, NETWORK_TRANSPORT, (_ed(flat, "Protocol") or "").lower() or None)
    _set(flat, SOURCE_IP, _ip(_ed(flat, "SourceIp")))
    _set(flat, SOURCE_PORT, _int(_ed(flat, "SourcePort")))
    _set(flat, SOURCE_DOMAIN, _ed(flat, "SourceHostname"))
    _set(flat, DESTINATION_IP, _ip(_ed(flat, "DestinationIp")))
    _set(flat, DESTINATION_PORT, _int(_ed(flat, "DestinationPort")))
    _set(flat, "destination.domain", _ed(flat, "DestinationHostname"))
    if code == "22":
        _sysmon_dns(flat)
    else:
        protocol = _ed(flat, "DestinationPortName") or _ed(flat, "SourcePortName")
        _set(flat, "network.protocol", protocol)
    _sysmon_flow(flat)


def _sysmon_user(flat: Flat) -> None:
    user = _ed(flat, "User")
    if user:
        domain, sep, name = user.partition("\\")
        if sep:
            _set(flat, "user.domain", domain)
            _set(flat, USER_NAME, name)
        else:
            _set(flat, USER_NAME, user)
        _append(flat, RELATED_USER, flat.get(USER_NAME))
    _set(flat, "user.id", flat.get("winlog.user.identifier"))
    for source, target in (
        ("Archived", "sysmon.file.archived"),
        ("IsExecutable", "sysmon.file.is_executable"),
    ):
        value = _ed(flat, source)
        if value is not None:
            flat[target] = value.lower() == "true"


def sysmon(flat: Flat) -> None:
    code = flat.get(EVENT_CODE)
    _apply_event_table(flat, _SYS_EVENTS, code)
    task = _SYSMON_TASKS.get(code or "")
    if task:
        flat[WINLOG_TASK] = task
        flat["event.action"] = task
    # Sysmon records when the activity happened; the log write time stays in event.created.
    utc_time = _ed(flat, "UtcTime")
    if utc_time:
        flat["@timestamp"] = utc_time
    rule = _ed(flat, "RuleName")
    if rule:
        _set(flat, "rule.name", rule)
    if code == "255":
        _set(flat, "error.code", _ed(flat, "ID"))
    if code == "25":
        _set(flat, "message", _ed(flat, "Type"))
    _sysmon_process(flat, code)
    _sysmon_files(flat)
    _sysmon_network(flat, code)
    event_type = _ed(flat, "EventType") or ""
    _registry(
        flat, _ed(flat, "TargetObject"), split_value=event_type in ("SetValue", "DeleteValue")
    )
    _registry_data(flat, _ed(flat, "Details"))
    _sysmon_user(flat)


# -- PowerShell ---------------------------------------------------------------------------

_PS_EVENTS: dict[str, dict[str, Any]] = {
    "400": {"category": ["process"], "type": ["start"], "action": "Engine Lifecycle"},
    "403": {"category": ["process"], "type": ["end"], "action": "Engine Lifecycle"},
    "600": {"category": ["process"], "type": ["info"], "action": "Provider Lifecycle"},
    "800": {"category": ["process"], "type": ["info"], "action": "Pipeline Execution Details"},
    "4103": {"category": ["process"], "type": ["info"], "action": "Executing Pipeline"},
    "4104": {"category": ["process"], "type": ["info"], "action": "Execute a Remote Command"},
    "4105": {"category": ["process"], "type": ["start"], "action": "Starting Command"},
    "4106": {"category": ["process"], "type": ["end"], "action": "Stopping Command"},
}
#: ``Key=Value`` / ``Key = Value`` context lines (spaces removed, lower-cased key) -> field.
_PS_KEYS = {
    "newenginestate": "powershell.engine.new_state",
    "previousenginestate": "powershell.engine.previous_state",
    "newproviderstate": "powershell.provider.new_state",
    "providername": "powershell.provider.name",
    "sequencenumber": "event.sequence",
    "detailsequence": "powershell.sequence",
    "detailtotal": "powershell.total",
    "hostname": "process.title",
    "hostversion": "powershell.process.executable_version",
    "hostid": "process.entity_id",
    "hostapplication": "process.command_line",
    "engineversion": "powershell.engine.version",
    "runspaceid": "powershell.runspace_id",
    "pipelineid": "powershell.pipeline_id",
    "commandname": "powershell.command.name",
    "commandtype": "powershell.command.type",
    "scriptname": FILE_PATH,
    "commandpath": "powershell.command.path",
    "commandline": "powershell.command.value",
    "shellid": "powershell.id",
    "userid": USER_NAME,
    "user": USER_NAME,
    "connecteduser": "powershell.connected_user.name",
}
_INVOCATION_KINDS = ("CommandInvocation", "ParameterBinding", "NonTerminatingError")


def _ps_lines(text: str) -> list[str]:
    unwrapped = text.replace("<string>", "\n").replace("</string>", "\n")
    return [line.rstrip("\r") for line in unwrapped.split("\n")]


def _ps_invocation(line: str) -> dict[str, str] | None:
    """``CommandInvocation(Add-Type): "Add-Type"`` -> detail dict, or None."""
    stripped = line.lstrip()
    kind = next((k for k in _INVOCATION_KINDS if stripped.startswith(k + "(")), None)
    if kind is None:
        return None
    related, sep, rest = stripped[len(kind) + 1 :].partition("):")
    if not sep:
        return None
    value = rest.lstrip()
    detail = {"type": kind, "related_command": related, "value": value}
    if kind == "ParameterBinding" and value.startswith("name="):
        name, sep, bound = value[5:].partition("; value=")
        if sep:
            detail["name"], detail["value"] = name, bound
    return detail


def _ps_context(text: str) -> dict[str, str]:
    """Context block -> {key: value}; keys lose spaces and case, values keep their whitespace."""
    out: dict[str, str] = {}
    for line in _ps_lines(text):
        if _ps_invocation(line) is not None:
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace(" ", "").isalpha():
            continue
        key = key.replace(" ", "").lower()
        out.setdefault(key, value if key == "commandline" else value.lstrip(" "))
    return out


def _split_user(flat: Flat, prefix: str) -> None:
    value = flat.get(f"{prefix}.name")
    if isinstance(value, str) and "\\" in value:
        domain, name = value.split("\\", 1)
        flat[f"{prefix}.domain"], flat[f"{prefix}.name"] = domain, name


def _powershell_events(flat: Flat, code: str | None, text: str) -> None:
    if code == "4104":
        _set(flat, "powershell.file.script_block_text", _ed(flat, "ScriptBlockText"))
        _set(flat, "powershell.file.script_block_id", _ed(flat, "ScriptBlockId"))
        _set(flat, "powershell.sequence", _ed(flat, "MessageNumber"))
        _set(flat, "powershell.total", _ed(flat, "MessageTotal"))
        _file_from_path(flat, _ed(flat, "Path"))
    if code in ("4105", "4106"):
        _set(flat, "powershell.file.script_block_id", _ed(flat, "ScriptBlockId"))
        _set(flat, "powershell.runspace_id", _ed(flat, "RunspaceId"))
    if code in ("800", "4103"):
        details = [d for d in (_ps_invocation(line) for line in _ps_lines(text)) if d]
        if details:
            flat["powershell.command.invocation_details"] = details


def _powershell_users(flat: Flat, code: str | None) -> None:
    _set(flat, "user.id", flat.get("winlog.user.identifier"))
    _split_user(flat, "user")
    _split_user(flat, "powershell.connected_user")
    if code == "4103":
        pairs = (("destination.user", "user"), ("source.user", "powershell.connected_user"))
        for prefix, source in pairs:
            for part in ("name", "domain"):
                _set(flat, f"{prefix}.{part}", flat.get(f"{source}.{part}"))
    _append(flat, RELATED_USER, flat.get(USER_NAME))


def powershell(flat: Flat) -> None:
    code = flat.get(EVENT_CODE)
    _apply_event_table(flat, _PS_EVENTS, code)
    if code in _PS_EVENTS:
        flat[WINLOG_TASK] = _PS_EVENTS[code]["action"]
    text = "\n".join(
        v for k, v in flat.items() if k.startswith(f"{EVENT_DATA}.") and isinstance(v, str)
    )
    context = _ps_context(text)
    for key, target in _PS_KEYS.items():
        value = context.get(key)
        if value is not None and value.strip():
            _set(flat, target, value)
    if flat.get(FILE_PATH):
        _file_from_path(flat, flat[FILE_PATH])
    _powershell_events(flat, code, text)
    _powershell_users(flat, code)
    command_line = flat.get("process.command_line")
    if isinstance(command_line, str):
        args = command_line.split()
        _set(flat, "process.args", args)
        _set(flat, "process.args_count", len(args))


MODULES = {"security": security, "sysmon": sysmon, "powershell": powershell}
