"""
Mock Salesforce Tooling/REST API, used to validate sf_client.py +
onboarding.py + the schema-v3 extractors end to end without a live org.

Exercises the v3 paths: NamespacePrefix / ApiVersion on Apex, Flow version/
status, a record-triggered after-save flow with entry filters, a decision
graph, a self-referential loop-guard record update, a structured action
call, plus an Apex class with a swallowed exception and an unpersisted field
write (mirrors the CNG_Trigger_Approvals findings in the v3 proposal).
"""
from fastapi import FastAPI, Request, HTTPException

app = FastAPI(title="Mock Salesforce")

EXPECTED_TOKEN = "mock-token-123"


# The real Tooling API serializes a Flow value node with EVERY union member
# present, the unused ones null. These helpers reproduce that exactly so the
# extractor is exercised against the real shape (this is what surfaced D-01).
def _vnode(**present):
    node = {"stringValue": None, "booleanValue": None, "numberValue": None,
            "dateValue": None, "dateTimeValue": None, "elementReference": None}
    node.update(present)
    return node

def SV(s): return _vnode(stringValue=s)
def BV(b): return _vnode(booleanValue=b)
def NV(n): return _vnode(numberValue=n)
def REF(r): return _vnode(elementReference=r)
def BLANK(): return _vnode()  # all members null -> deliberate blank assignment
def WEIRD(): return {"stringValue": None, "booleanValue": None, "unmodeledValue": "xyz"}

# Never-cleared static map bug (v2 case) -- persisted write.
BUGGY_CLASS_BODY = """
public with sharing class MockPricingCallback {
    public static Map<Decimal, Decimal> lineAdjustmentCache = new Map<Decimal, Decimal>();

    public static void applyAdjustments(List<Mock_Line__c> lines) {
        for (Mock_Line__c item : lines) {
            if (lineAdjustmentCache.containsKey(item.Line_Number__c)) {
                item.Increment_Adjustment__c = lineAdjustmentCache.get(item.Line_Number__c);
            }
        }
        update lines;
    }
}
"""

# Swallowed exception + unpersisted field write + ORDER BY SOQL (v3 case).
APPROVAL_CLASS_BODY = """
public class CNG_Trigger_Approvals {
    @InvocableMethod(label='Trigger Approval Requests')
    public static void TriggerApprovalRequests(List<Id> agreementIds) {
        List<Apttus_Approval__Approval_Process__c> procs = [
            SELECT Apttus_Approval__Sequence__c, Id
            FROM Apttus_Approval__Approval_Process__c
            WHERE Apttus_Approval__Process_Name__c != null
            ORDER BY Apttus_Approval__Sequence__c ASC
        ];
        Mock_Line__c AgmtRec = new Mock_Line__c();
        AgmtRec.Auto_Trigger_Approvals__c = false;
        try {
            ApprovalsWebService.submitForApproval(agreementIds);
        } catch (Exception e) {
            System.debug('approval failed: ' + e.getMessage());
        }
    }
}
"""

TRIGGER_BODY = """
trigger MockLineTrigger on Mock_Line__c (before update) {
    if (Trigger.isBefore && Trigger.isUpdate) {
        MockPricingCallback.applyAdjustments(Trigger.new);
    }
}
"""

# Record-triggered after-save flow on Mock_Line__c with entry filter, a
# decision, a self-referential record update that clears the trigger flag
# (loop guard), and an apex action call.
FLOW_METADATA = {
    "label": "Mock Line Update Flow",
    "processType": "AutoLaunchedFlow",
    "description": "Triggers approvals for mock lines",
    "runInMode": "DefaultMode",
    "start": {
        "object": "Mock_Line__c",
        "triggerType": "RecordAfterSave",
        "recordTriggerType": "Update",
        "filterLogic": "and",
        "doesRequireRecordChangedToMeetCriteria": True,
        "filters": [
            {"field": "Auto_Trigger_Approvals__c", "operator": "EqualTo", "value": BV(True)},
            {"field": "CNG_Record_Type_Name__c", "operator": "EqualTo", "value": SV("Physician")},
        ],
        "connector": {"targetReference": "Route_By_Next_Approval"},
    },
    "decisions": [{
        "name": "Route_By_Next_Approval", "label": "Route By Next Approval",
        "rules": [{
            "name": "Is_Recruitment", "label": "Is Recruitment", "conditionLogic": "and",
            "conditions": [{"field": "Next_Approval_to_Trigger__c", "operator": "EqualTo",
                            "value": SV("Recruitment")}],
            "connector": {"targetReference": "Trigger_Approvals"},
        }],
        "defaultConnector": {"targetReference": "APPRO_update"},
    }],
    "actionCalls": [{
        "name": "Trigger_Approvals", "actionType": "apex", "actionName": "CNG_Trigger_Approvals",
        "inputParameters": [{"name": "agreementIds", "value": REF("$Record.Id")}],
        "connector": {"targetReference": "APPRO_update"},
        "faultConnector": {"targetReference": "Approval_Failed_Path"},
    }],
    "recordUpdates": [{
        "name": "APPRO_update", "inputReference": "$Record",   # no explicit object -> Q-07 derives it
        "inputAssignments": [
            {"field": "Apttus__Status__c", "value": SV("Activated")},          # STRING must survive (D-01)
            {"field": "Amount__c", "value": NV(100)},                          # NUMBER must survive
            {"field": "Owner_Ref__c", "value": REF("varOwner.Id")},            # REFERENCE must survive
            {"field": "Auto_Trigger_Approvals__c", "value": BV(False)},        # boolean false (loop guard)
            {"field": "Next_Approval_to_Trigger__c", "value": BLANK()},        # deliberate blank
            {"field": "Weird_Field__c", "value": WEIRD()},                     # -> unparsed (D-02)
        ],
        # no faultConnector -> should raise the no_fault_path flag
    }],
    "assignments": [{
        "name": "SetAdjustment",
        "assignmentItems": [{"assignToReference": "$Record.Next_Approval_to_Trigger__c",
                             "operator": "Assign", "value": BLANK()}],
        "connector": {"targetReference": "Route_By_Next_Approval"},
    }],
}

WORKFLOW_FIELD_UPDATES = [
    {"Id": "04Y001", "FullName": "Mock_Line__c.Reset_Increment_Adjustment",
     "Metadata": {"field": "Increment_Adjustment__c", "name": "Reset Increment Adjustment",
                  "operation": "Literal", "literalValue": "0", "reevaluateOnChange": True}},
]

LWC_FILES = {
    "mockLineEditor": {
        "mockLineEditor.js": "import { LightningElement, api } from 'lwc';\n"
                              "export default class MockLineEditor extends LightningElement {\n"
                              "  @api recordId;\n}\n",
        "mockLineEditor.html": "<template></template>",
    }
}


@app.middleware("http")
async def _concurrency_cap(request, call_next):
    from fastapi.responses import JSONResponse
    _IN_FLIGHT[0] += 1
    STATS["peak_in_flight"] = max(STATS["peak_in_flight"], _IN_FLIGHT[0])
    try:
        if CONCURRENCY_CAP is not None and _IN_FLIGHT[0] > CONCURRENCY_CAP:
            STATS["throttled"] += 1
            return JSONResponse(status_code=403, content=[{
                "message": "ConcurrentRequests (Concurrent API Requests) Limit exceeded.",
                "errorCode": "REQUEST_LIMIT_EXCEEDED"}])
        return await call_next(request)
    finally:
        _IN_FLIGHT[0] -= 1


def _check_auth(request: Request):
    if request.headers.get("authorization", "") != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(401, "invalid token")


@app.get("/services/data/v60.0/limits")
def limits(request: Request):
    _check_auth(request)
    return {"DailyApiRequests": {"Max": 100000, "Remaining": 99000}}


@app.get("/services/data/v60.0/sobjects/")
def sobjects(request: Request):
    _check_auth(request)
    return {"sobjects": [
        {"name": "Mock_Line__c", "custom": True},
        {"name": "Apttus_Approval__Approval_Process__c", "custom": True},
        {"name": "Account", "custom": False},
    ]}


# The FinaliseCartBatch -> C2C_ActivateCPIOrderQueuable pair from Equiniti:
# the queueable is started from a batch finish() with a delay and has no
# `X.method(` caller anywhere -- the case that used to produce no inbound edge.
FINALISE_BATCH_BODY = """
public with sharing class FinaliseCartBatch implements Database.Batchable<Id>, Database.Stateful {
    private Map<Id,Id> mapOfCartToOrder;
    public FinaliseCartBatch(Map<Id,Id> cartToOrderMap){ this.mapOfCartToOrder = cartToOrderMap; }
    public Iterable<Id> start(Database.BatchableContext batchableContext){
        return new List<Id>(this.mapOfCartToOrder.keySet());
    }
    public void execute(Database.BatchableContext batchableContext, List<Id> scope){ }
    public void finish(Database.BatchableContext batchableContext){
        if(this.mapOfCartToOrder.values().isEmpty()){
            return;
        }

        Integer delayInMinutes = 2;

        System.enqueueJob(new C2C_ActivateCPIOrderQueuable(new Set<Id>(this.mapOfCartToOrder.values())), delayInMinutes);
    }
}
"""

QUEUEABLE_BODY = """
public with sharing class C2C_ActivateCPIOrderQueuable implements Database.AllowsCallouts, Queueable {
    private Set<Id> setOfOrderIds;
    public C2C_ActivateCPIOrderQueuable(Set<Id> setOfOrderIds){ this.setOfOrderIds = setOfOrderIds; }
    public void execute(QueueableContext context){
        try { } catch (Exception e) { }
    }
}
"""

# Optional artificial latency per request (seconds), so a benchmark can show
# what parallel fetching buys against a network that is not a loopback.
LATENCY = 0.0
REQUEST_LOG = []
FAIL_NEXT_BODY_QUERIES = 0      # answer this many Body queries with a 503 first
ORG_NAMESPACE = None            # the org's own namespace (Organization.NamespacePrefix)
COMPOSITE_SUPPORTED = True      # False -> /tooling/composite answers 404
# Simulate Salesforce's concurrent-request cap: more than this many requests
# in flight at once get the 403 REQUEST_LIMIT_EXCEEDED ConcurrentRequests.
CONCURRENCY_CAP = None
_IN_FLIGHT = [0]
STATS = {"peak_in_flight": 0, "throttled": 0, "composite_calls": 0, "flow_gets": 0}
REJECT_BODY_QUERIES = False     # answer Body queries with a 401 (token revoked mid-fetch)


def _apex_class_records():
    # read at request time: test_refresh_e2e rebinds BUGGY_CLASS_BODY
    return [
        {"Id": "01p001", "Name": "MockPricingCallback", "NamespacePrefix": None,
         "ApiVersion": 60.0, "Body": BUGGY_CLASS_BODY},
        {"Id": "01p002", "Name": "CNG_Trigger_Approvals", "NamespacePrefix": None,
         "ApiVersion": 64.0, "Body": APPROVAL_CLASS_BODY},
        {"Id": "01p003", "Name": "ApttusInternalHelper", "NamespacePrefix": "Apttus",
         "ApiVersion": 58.0, "Body": "global class ApttusInternalHelper { public void x(){} }"},
        {"Id": "01p004", "Name": "FinaliseCartBatch", "NamespacePrefix": None,
         "ApiVersion": 61.0, "Body": FINALISE_BATCH_BODY},
        {"Id": "01p005", "Name": "C2C_ActivateCPIOrderQueuable", "NamespacePrefix": None,
         "ApiVersion": 59.0, "Body": QUEUEABLE_BODY},
        {"Id": "01p006", "Name": "PricingCallbackBase", "NamespacePrefix": "Apttus_Config2",
         "ApiVersion": 58.0, "Body": "(hidden)"},
        {"Id": "01p007", "Name": "OwnNsHelper", "NamespacePrefix": "myns",
         "ApiVersion": 60.0, "Body": "public class OwnNsHelper { public static void go(){ PricingCallbackBase.run(); } }"},
    ] + EXTRA_CLASSES


EXTRA_CLASSES = []   # a benchmark can pad the org with synthetic classes
EXTRA_FLOWS = []     # ... and with extra FlowDefinitions (served FLOW_METADATA)


def _apex_trigger_records():
    return [
        {"Id": "01q001", "Name": "MockLineTrigger", "NamespacePrefix": None, "ApiVersion": 60.0,
         "Body": TRIGGER_BODY, "TableEnumOrId": "Mock_Line__c"},
        {"Id": "01q002", "Name": "ProductConfigurationTrigger", "NamespacePrefix": "Apttus_Config2",
         "ApiVersion": 58.0, "Body": "(hidden)", "TableEnumOrId": "Account"},
    ]


def _ids_in(q):
    import re
    m = re.search(r"\bIN\s*\(([^)]*)\)", q, re.IGNORECASE)
    if not m:
        return None
    return {x.strip().strip("'") for x in m.group(1).split(",") if x.strip()}


def _project(records, q):
    """Honour the parts of the SOQL the client relies on: an Id / bundle Id
    IN (...) filter, and a Body-less listing (plus LengthWithoutComments)."""
    ids = _ids_in(q)
    if ids is not None:
        key = "LightningComponentBundleId" if "LIGHTNINGCOMPONENTBUNDLEID IN" in q.upper() else "Id"
        records = [r for r in records if r.get(key) in ids]
    select = q.upper().split(" FROM ")[0]
    if "BODY" not in select and records and "Body" in records[0]:
        out = []
        for r in records:
            r2 = {k: v for k, v in r.items() if k != "Body"}
            if "LENGTHWITHOUTCOMMENTS" in select:
                r2["LengthWithoutComments"] = len(r.get("Body") or "")
            out.append(r2)
        records = out
    return records


@app.get("/services/data/v60.0/tooling/query/")
async def tooling_query(request: Request, q: str):
    _check_auth(request)
    REQUEST_LOG.append(q[:160])
    if LATENCY:
        import asyncio
        await asyncio.sleep(LATENCY)
    ql = q.upper()
    global FAIL_NEXT_BODY_QUERIES
    if "BODY" in ql.split(" FROM ")[0] and "FROM APEXCLASS" in ql:
        if REJECT_BODY_QUERIES:
            raise HTTPException(401, "session expired")
        if FAIL_NEXT_BODY_QUERIES > 0:
            FAIL_NEXT_BODY_QUERIES -= 1
            raise HTTPException(503, "server busy")
    if "FROM APEXCLASS" in ql:
        return {"records": _project(_apex_class_records(), q), "nextRecordsUrl": None}
    if "FROM APEXTRIGGER" in ql:
        return {"records": _project(_apex_trigger_records(), q), "nextRecordsUrl": None}
    if "FROM FLOWDEFINITION" in ql:
        return {"records": [
            {"Id": "300001", "DeveloperName": "Mock_Line_Update_Flow", "NamespacePrefix": None,
             "ActiveVersionId": "301001",
             "ActiveVersion": {"VersionNumber": 7, "ApiVersion": 64.0, "Status": "Active"}},
        ] + EXTRA_FLOWS, "nextRecordsUrl": None}
    if "FROM LIGHTNINGCOMPONENTBUNDLE" in ql:
        return {"records": [{"Id": "0Rb001", "DeveloperName": "mockLineEditor", "NamespacePrefix": None},
                            {"Id": "0Rb002", "DeveloperName": "cartGrid", "NamespacePrefix": "Apttus_Config2"}],
                "nextRecordsUrl": None}
    if "FROM LIGHTNINGCOMPONENTRESOURCE" in ql:
        import base64
        rows = [{"LightningComponentBundleId": "0Rb001", "FilePath": f"lwc/mockLineEditor/{fn}",
                 "Source": base64.b64encode(c.encode()).decode()}
                for fn, c in LWC_FILES["mockLineEditor"].items()]
        return {"records": _project(rows, q), "nextRecordsUrl": None}
    if "FROM WORKFLOWFIELDUPDATE" in ql:
        return {"records": WORKFLOW_FIELD_UPDATES, "nextRecordsUrl": None}
    return {"records": [], "nextRecordsUrl": None}


@app.get("/services/data/v60.0/query/")
def rest_query(request: Request, q: str):
    _check_auth(request)
    if "FROM ORGANIZATION" in q.upper():
        return {"records": [{"NamespacePrefix": ORG_NAMESPACE}], "nextRecordsUrl": None}
    return {"records": [], "nextRecordsUrl": None}


@app.post("/services/data/v60.0/tooling/composite")
async def tooling_composite(request: Request):
    _check_auth(request)
    if not COMPOSITE_SUPPORTED:
        raise HTTPException(404, "The requested resource does not exist")
    STATS["composite_calls"] += 1
    if LATENCY:
        import asyncio
        await asyncio.sleep(LATENCY)
    body = await request.json()
    out = []
    for sub in body.get("compositeRequest", []):
        url = sub.get("url", "")
        if "/tooling/sobjects/Flow/" in url and url.rsplit("/", 1)[-1] != "BROKEN":
            out.append({"body": {"Metadata": FLOW_METADATA}, "httpStatusCode": 200,
                        "referenceId": sub.get("referenceId")})
        else:
            out.append({"body": [{"errorCode": "NOT_FOUND", "message": "nope"}], "httpStatusCode": 404,
                        "referenceId": sub.get("referenceId")})
    return {"compositeResponse": out}


@app.get("/services/data/v60.0/tooling/sobjects/Flow/{version_id}")
async def flow_metadata(version_id: str, request: Request):
    _check_auth(request)
    STATS["flow_gets"] += 1
    if LATENCY:
        import asyncio
        await asyncio.sleep(LATENCY)
    return {"Metadata": FLOW_METADATA}
