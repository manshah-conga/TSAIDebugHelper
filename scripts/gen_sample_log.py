"""
Generates a synthetic-but-realistic Apex debug log so normalize_log.py can
be validated end-to-end. No customer debug log was supplied for this
engagement yet; this file follows the standard Salesforce debug log event
grammar (timestamp (nanos)|EVENT|...) and reuses real class/trigger names
found in this org's backup (ibmcAgreementTriggerHelper /
ibmcCampaignContactTriggerHandler) so the RCA context assembler demo has
something real to cross-reference against org_index.json. Replace this
with an actual customer log as soon as one is available.
"""
lines = []
lines.append("52.0 APEX_CODE,FINEST;APEX_PROFILING,NONE;CALLOUT,INFO;DB,INFO;SYSTEM,DEBUG;VALIDATION,INFO;WORKFLOW,INFO")
lines.append("13:45:01.100 (1000000)|EXECUTION_STARTED")
lines.append("13:45:01.101 (1010000)|CODE_UNIT_STARTED|[EXTERNAL]|01p000000000001|ibmcCampaignContactTrigger on ibmcCampaignContact__c trigger event BeforeInsert")
lines.append("13:45:01.102 (1020000)|METHOD_ENTRY|[10]|01p000000000002|ibmcCampaignContactTriggerHandler.beforeInsert(List<ibmcCampaignContact__c>)")

# Loop of 50 identical-shape SOQL queries + DML - the anti-pattern we want collapsed
for i in range(50):
    lines.append(f"13:45:01.{110+i:03d} (11{i:05d})|SOQL_EXECUTE_BEGIN|[15]|Aggregations:0|SELECT Id, ibmcCtryISOCdTxt__c FROM Country__c WHERE Id = '00100000000{i:02d}'")
    lines.append(f"13:45:01.{111+i:03d} (11{i:05d})|SOQL_EXECUTE_END|[15]|Rows:1")
    lines.append(f"13:45:01.{112+i:03d} (11{i:05d})|DML_BEGIN|[20]|Op:Update|Type:ibmcCampaignContact__c|Rows:1")
    lines.append(f"13:45:01.{113+i:03d} (11{i:05d})|DML_END|[20]")

lines.append("13:45:01.900 (1900000)|METHOD_EXIT|[10]|01p000000000002|ibmcCampaignContactTriggerHandler.beforeInsert(List<ibmcCampaignContact__c>)")
lines.append("13:45:01.901 (1910000)|CODE_UNIT_FINISHED|ibmcCampaignContactTrigger on ibmcCampaignContact__c trigger event BeforeInsert")

# Second code unit: the one that actually fails - ibmcAgreementTriggerHelper.createShareAgreements
lines.append("13:45:02.000 (2000000)|CODE_UNIT_STARTED|[EXTERNAL]|01p000000000003|ibmcAgreementTrigger on Apttus__APTS_Agreement__c trigger event AfterUpdate")
lines.append("13:45:02.001 (2010000)|METHOD_ENTRY|[30]|01p000000000004|ibmcAgreementTriggerHelper.createShareAgreements(List<Apttus__APTS_Agreement__c>, Map<Id,RecordTypeInfo>)")
lines.append("13:45:02.010 (2020000)|SOQL_EXECUTE_BEGIN|[40]|Aggregations:0|SELECT Id, profileId, profile.name, userRoleId, UserRole.Name FROM User WHERE Id IN :userIds")
lines.append("13:45:02.015 (2030000)|SOQL_EXECUTE_END|[40]|Rows:3")
lines.append("13:45:02.020 (2040000)|CALLOUT_REQUEST|[55]|System.HttpRequest[Endpoint=callout:IBM_Hub_API/v1/sync, Method=POST]")
lines.append("13:45:02.180 (2050000)|CALLOUT_RESPONSE|[55]|System.HttpResponse[Status=200, StatusCode=200]")
lines.append("13:45:02.200 (2060000)|USER_DEBUG|[58]|DEBUG|owner profile name lookup starting for agreement 0013X00000AbCdeQAX")
lines.append("13:45:02.210 (2070000)|EXCEPTION_THROWN|[62]|System.NullPointerException: Attempt to de-reference a null object")
lines.append("13:45:02.211 (2070100)|FATAL_ERROR|System.NullPointerException: Attempt to de-reference a null object")
lines.append("Class.ibmcAgreementTriggerHelper.createShareAgreements: line 62, column 1")
lines.append("Trigger.ibmcAgreementTrigger: line 4, column 1")
lines.append("13:45:02.212 (2070200)|METHOD_EXIT|[30]|01p000000000004|ibmcAgreementTriggerHelper.createShareAgreements(List<Apttus__APTS_Agreement__c>, Map<Id,RecordTypeInfo>)")
lines.append("13:45:02.213 (2070300)|CODE_UNIT_FINISHED|ibmcAgreementTrigger on Apttus__APTS_Agreement__c trigger event AfterUpdate")

lines.append("13:45:02.300 (2100000)|CUMULATIVE_LIMIT_USAGE")
lines.append("13:45:02.300 (2100100)|LIMIT_USAGE_FOR_NS|(default)|")
lines.append("  Number of SOQL queries: 51 out of 100")
lines.append("  Number of query rows: 53 out of 50000")
lines.append("  Number of SOSL queries: 0 out of 20")
lines.append("  Number of DML statements: 50 out of 150")
lines.append("  Number of DML rows: 50 out of 10000")
lines.append("  Number of CPU time (milliseconds): 812 out of 10000")
lines.append("  Number of callouts: 1 out of 100")
lines.append("13:45:02.301 (2100200)|CUMULATIVE_LIMIT_USAGE_END")
lines.append("13:45:02.302 (2100300)|EXECUTION_FINISHED")

with open("sample_debug_log.log", "w") as f:
    f.write("\n".join(lines) + "\n")

print(f"Wrote {len(lines)} lines")
