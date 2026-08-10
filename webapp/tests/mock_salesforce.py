"""
Mock Salesforce Tooling/REST API, used only to validate sf_client.py +
onboarding.py end to end without a live org. Mirrors the exact endpoints
sf_client.py calls: /services/data/vXX/limits, /sobjects/,
/tooling/query/?q=..., /tooling/sobjects/Flow/<id>.

Includes one Apex class deliberately written with the "never-cleared
static Map" anti-pattern (mirrors the real CPQ_PricingCallBack bug found
earlier in this project) so the risk-detection path can be verified
end-to-end, not just unit-tested in isolation.

Run with: uvicorn tests.mock_salesforce:app --port 8001
"""
from fastapi import FastAPI, Request, HTTPException

app = FastAPI(title="Mock Salesforce")

EXPECTED_TOKEN = "mock-token-123"

BUGGY_CLASS_BODY = """
public class MockPricingCallback {
    public static Map<Decimal, Decimal> lineAdjustmentCache = new Map<Decimal, Decimal>();

    public static void applyAdjustments(List<Mock_Line__c> lines) {
        for (Mock_Line__c item : lines) {
            if (lineAdjustmentCache.containsKey(item.Line_Number__c)) {
                item.Increment_Adjustment__c = lineAdjustmentCache.get(item.Line_Number__c);
            }
        }
        update lines;
    }

    public static void cacheAdjustment(Decimal lineNumber, Decimal value) {
        lineAdjustmentCache.put(lineNumber, value);
    }
}
"""

NORMAL_CLASS_BODY = """
public class MockQuoteHelper {
    public static void recalcTotals(List<Mock_Line__c> lines) {
        Decimal total = 0;
        for (Mock_Line__c item : lines) {
            total += item.Amount__c;
        }
        System.debug('total=' + total);
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

FLOW_METADATA = {
    "label": "Mock Line Update Flow",
    "processType": "AutoLaunchedFlow",
    "start": {"object": "Mock_Line__c", "triggerType": "RecordAfterSave", "recordTriggerType": "Update"},
    "actionCalls": [{"actionName": "MockPricingCallback", "actionType": "apex"}],
    "subflows": [],
    "decisions": [{"name": "CheckAmount"}],
    "assignments": [{"name": "SetAdjustment", "assignmentItems": [
        {"assignToReference": "$Record.Increment_Adjustment__c",
         "operator": "Assign", "value": {"numberValue": 0}},
    ]}],
    "recordUpdates": [{
        "name": "UpdateLine", "object": "Mock_Line__c", "inputReference": "$Record",
        "inputAssignments": [
            {"field": "Increment_Adjustment__c", "value": {"elementReference": "SetAdjustment"}},
        ],
    }],
    "faultConnectors": [],
}

# A Workflow Rule / Approval Process field update that writes the SAME field,
# so validation confirms all three mechanisms surface for one field.
WORKFLOW_FIELD_UPDATES = [
    {"Id": "04Y001", "FullName": "Mock_Line__c.Reset_Increment_Adjustment",
     "Metadata": {"field": "Increment_Adjustment__c", "name": "Reset Increment Adjustment",
                  "operation": "Literal", "literalValue": "0", "reevaluateOnChange": True}},
]

LWC_FILES = {
    "mockLineEditor": {
        "mockLineEditor.js": "import { LightningElement, api } from 'lwc';\n"
                              "export default class MockLineEditor extends LightningElement {\n"
                              "  @api recordId;\n"
                              "  connectedCallback() { console.log('loaded', this.recordId); }\n"
                              "}\n",
        "mockLineEditor.html": "<template><lightning-card title='Mock Line Editor'></lightning-card></template>",
        "mockLineEditor.js-meta.xml": "<LightningComponentBundle><isExposed>true</isExposed></LightningComponentBundle>",
    }
}


def _check_auth(request: Request):
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {EXPECTED_TOKEN}":
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
        {"name": "Account", "custom": False},
    ]}


@app.get("/services/data/v60.0/tooling/query/")
def tooling_query(request: Request, q: str):
    _check_auth(request)
    ql = q.upper()
    if "FROM APEXCLASS" in ql:
        return {"records": [
            {"Id": "01p001", "Name": "MockPricingCallback", "Body": BUGGY_CLASS_BODY},
            {"Id": "01p002", "Name": "MockQuoteHelper", "Body": NORMAL_CLASS_BODY},
        ], "nextRecordsUrl": None}
    if "FROM APEXTRIGGER" in ql:
        return {"records": [
            {"Id": "01q001", "Name": "MockLineTrigger", "Body": TRIGGER_BODY, "TableEnumOrId": "Mock_Line__c"},
        ], "nextRecordsUrl": None}
    if "FROM FLOWDEFINITION" in ql:
        return {"records": [
            {"Id": "300001", "DeveloperName": "Mock_Line_Update_Flow", "ActiveVersionId": "301001",
             "ActiveVersion": {"VersionNumber": 3, "ApiVersion": 60.0}},
        ], "nextRecordsUrl": None}
    if "FROM LIGHTNINGCOMPONENTBUNDLE" in ql:
        return {"records": [
            {"Id": "0Rb001", "DeveloperName": "mockLineEditor"},
        ], "nextRecordsUrl": None}
    if "FROM LIGHTNINGCOMPONENTRESOURCE" in ql:
        import base64
        files = LWC_FILES["mockLineEditor"]
        records = [
            {"FilePath": f"lwc/mockLineEditor/{fn}", "Source": base64.b64encode(content.encode()).decode()}
            for fn, content in files.items()
        ]
        return {"records": records, "nextRecordsUrl": None}
    if "FROM WORKFLOWFIELDUPDATE" in ql:
        # Emulates a bulk Metadata query being available (some orgs allow it).
        return {"records": WORKFLOW_FIELD_UPDATES, "nextRecordsUrl": None}
    return {"records": [], "nextRecordsUrl": None}


@app.get("/services/data/v60.0/tooling/sobjects/Flow/{version_id}")
def flow_metadata(version_id: str, request: Request):
    _check_auth(request)
    return {"Metadata": FLOW_METADATA}
