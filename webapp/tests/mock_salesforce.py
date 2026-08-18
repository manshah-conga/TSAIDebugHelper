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


@app.get("/services/data/v60.0/tooling/query/")
def tooling_query(request: Request, q: str):
    _check_auth(request)
    ql = q.upper()
    if "FROM APEXCLASS" in ql:
        return {"records": [
            {"Id": "01p001", "Name": "MockPricingCallback", "NamespacePrefix": None,
             "ApiVersion": 60.0, "Body": BUGGY_CLASS_BODY},
            {"Id": "01p002", "Name": "CNG_Trigger_Approvals", "NamespacePrefix": None,
             "ApiVersion": 64.0, "Body": APPROVAL_CLASS_BODY},
            {"Id": "01p003", "Name": "ApttusInternalHelper", "NamespacePrefix": "Apttus",
             "ApiVersion": 58.0, "Body": "global class ApttusInternalHelper { public void x(){} }"},
        ], "nextRecordsUrl": None}
    if "FROM APEXTRIGGER" in ql:
        return {"records": [
            {"Id": "01q001", "Name": "MockLineTrigger", "NamespacePrefix": None, "ApiVersion": 60.0,
             "Body": TRIGGER_BODY, "TableEnumOrId": "Mock_Line__c"},
        ], "nextRecordsUrl": None}
    if "FROM FLOWDEFINITION" in ql:
        return {"records": [
            {"Id": "300001", "DeveloperName": "Mock_Line_Update_Flow", "NamespacePrefix": None,
             "ActiveVersionId": "301001",
             "ActiveVersion": {"VersionNumber": 7, "ApiVersion": 64.0, "Status": "Active"}},
        ], "nextRecordsUrl": None}
    if "FROM LIGHTNINGCOMPONENTBUNDLE" in ql:
        return {"records": [{"Id": "0Rb001", "DeveloperName": "mockLineEditor"}], "nextRecordsUrl": None}
    if "FROM LIGHTNINGCOMPONENTRESOURCE" in ql:
        import base64
        return {"records": [
            {"FilePath": f"lwc/mockLineEditor/{fn}", "Source": base64.b64encode(c.encode()).decode()}
            for fn, c in LWC_FILES["mockLineEditor"].items()
        ], "nextRecordsUrl": None}
    if "FROM WORKFLOWFIELDUPDATE" in ql:
        return {"records": WORKFLOW_FIELD_UPDATES, "nextRecordsUrl": None}
    return {"records": [], "nextRecordsUrl": None}


@app.get("/services/data/v60.0/tooling/sobjects/Flow/{version_id}")
def flow_metadata(version_id: str, request: Request):
    _check_auth(request)
    return {"Metadata": FLOW_METADATA}
