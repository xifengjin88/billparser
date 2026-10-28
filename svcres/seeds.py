"""Hand-maintained seed data that the public catalogs don't provide.

- A shared category list, so AWS, Azure and GCP roll up the same way.
- AWS categories and common aliases (the AWS price index has codes, not categories).
- Aliases seen on invoices and Cost Explorer exports ("EC2-Other", "EC2 - Instances").
- Keyword rules to categorize GCP services.

Edit freely. Anything learned from corrections is a synonym instead (billparse: table service_synonym).
"""

CATEGORIES = [
    "Compute", "Storage", "Databases", "Networking", "Analytics",
    "AI + Machine Learning", "Containers", "Security", "Management",
    "Developer Tools", "Integration", "Support", "Marketplace", "Other",
]

# Azure serviceFamily -> shared category
AZURE_FAMILY_TO_CATEGORY = {
    "Compute": "Compute",
    "Storage": "Storage",
    "Databases": "Databases",
    "Networking": "Networking",
    "Analytics": "Analytics",
    "AI + Machine Learning": "AI + Machine Learning",
    "Containers": "Containers",
    "Security": "Security",
    "Management and Governance": "Management",
    "Developer Tools": "Developer Tools",
    "Integration": "Integration",
    "Web": "Compute",
    "Windows Virtual Desktop": "Compute",
    "Internet of Things": "Integration",
    "Data": "Analytics",
    "Identity": "Security",
    "Mixed Reality": "Other",
}

# AWS service code -> (category, extra aliases). Names come from the fetch script.
AWS_SEED = {
    # the Pricing Calculator lists EC2 with a Windows or SQL Server license as its own service, and EBS apart
    "AmazonEC2": ("Compute", ["EC2", "EC2-Instances", "EC2 - Instances", "EC2-Other", "EC2 - Other",
                              "Windows Server and SQL Server on Amazon EC2", "Amazon Elastic Block Store (EBS)",
                              "Amazon Elastic Block Store", "Elastic Block Store", "EBS",
                              "Amazon Elastic Compute Cloud - Compute", "Elastic Compute Cloud"]),
    "AWSLambda": ("Compute", ["Lambda", "AWS Lambda"]),
    "AmazonLightsail": ("Compute", ["Lightsail"]),
    "AmazonECS": ("Containers", ["ECS", "Elastic Container Service", "Fargate", "AWS Fargate"]),
    "AmazonEKS": ("Containers", ["EKS", "Elastic Kubernetes Service"]),
    "AmazonECR": ("Containers", ["ECR", "Elastic Container Registry"]),
    "AmazonS3": ("Storage", ["S3", "Simple Storage Service"]),
    "AmazonEFS": ("Storage", ["EFS", "Elastic File System"]),
    "AmazonFSx": ("Storage", ["FSx"]),
    "AmazonS3GlacierDeepArchive": ("Storage", ["Glacier Deep Archive"]),
    "AmazonGlacier": ("Storage", ["Glacier", "S3 Glacier"]),
    "AWSBackup": ("Storage", ["AWS Backup", "Backup"]),
    "AmazonRDS": ("Databases", ["RDS", "Relational Database Service", "Aurora", "Amazon Aurora"]),
    "AmazonDynamoDB": ("Databases", ["DynamoDB"]),
    "AmazonElastiCache": ("Databases", ["ElastiCache"]),
    "AmazonRedshift": ("Analytics", ["Redshift"]),
    "AmazonDocDB": ("Databases", ["DocumentDB"]),
    "AmazonNeptune": ("Databases", ["Neptune"]),
    "AmazonES": ("Analytics", ["OpenSearch", "Elasticsearch Service", "Amazon Elasticsearch Service"]),
    "AmazonAthena": ("Analytics", ["Athena"]),
    "AWSGlue": ("Analytics", ["Glue", "AWS Glue"]),
    "ElasticMapReduce": ("Analytics", ["EMR", "Elastic MapReduce"]),
    "AmazonKinesis": ("Analytics", ["Kinesis"]),
    "AmazonMSK": ("Analytics", ["MSK", "Managed Streaming for Apache Kafka"]),
    "AmazonQuickSight": ("Analytics", ["QuickSight"]),
    "AmazonSageMaker": ("AI + Machine Learning", ["SageMaker"]),
    "AmazonBedrock": ("AI + Machine Learning", ["Bedrock"]),
    "AmazonVPC": ("Networking", ["VPC", "Virtual Private Cloud", "NAT Gateway", "Transit Gateway",
                                 "AWS Transit Gateway"]),
    "AmazonCloudFront": ("Networking", ["CloudFront"]),
    "AmazonRoute53": ("Networking", ["Route 53", "Route53"]),
    # the AWS Partner Network yearly fee ("APN Annual Program Fee"): a fee AWS bills itself, not usage, not Marketplace.
    # Our own code (not checked against a CUR product code). Not "APN" alone: APN1-3 are region codes in usage types.
    "AWSPartnerNetwork": ("Other", ["AWS Partner Network", "APN Annual Program Fee", "APN Program Fee"]),
    # domain registration and renewal: not in the price index; bills print "Registrar", "Amazon Registrar RenewDomain"
    "AmazonRegistrar": ("Networking", ["Amazon Registrar", "Registrar", "Route 53 Domains",
                                       "Amazon Route 53 Domains"]),
    "AWSELB": ("Networking", ["ELB", "Elastic Load Balancing", "Load Balancer"]),
    "AWSDataTransfer": ("Networking", ["Data Transfer", "AWS Data Transfer"]),
    "AWSDirectConnect": ("Networking", ["Direct Connect"]),
    "AmazonApiGateway": ("Networking", ["API Gateway"]),
    "AmazonCloudWatch": ("Management", ["CloudWatch"]),
    "AWSCloudTrail": ("Management", ["CloudTrail"]),
    "AWSConfig": ("Management", ["AWS Config", "Config"]),
    "AWSSystemsManager": ("Management", ["Systems Manager", "SSM"]),
    "awskms": ("Security", ["KMS", "Key Management Service"]),
    "AWSSecretsManager": ("Security", ["Secrets Manager"]),
    "AmazonGuardDuty": ("Security", ["GuardDuty"]),
    "awswaf": ("Security", ["WAF", "AWS WAF", "AWS Web Application Firewall (WAF)", "AWS Web Application Firewall"]),
    "AWSSecurityHub": ("Security", ["Security Hub"]),
    "AmazonSNS": ("Integration", ["SNS", "Simple Notification Service"]),
    "AWSQueueService": ("Integration", ["SQS", "Simple Queue Service"]),
    "AWSEvents": ("Integration", ["EventBridge", "CloudWatch Events"]),
    "AmazonStates": ("Integration", ["Step Functions"]),
    "AmazonSES": ("Integration", ["SES", "Simple Email Service"]),
    "AWSSupportBusiness": ("Support", ["AWS Support (Business)", "Business Support"]),
    "AWSSupportEnterprise": ("Support", ["AWS Support (Enterprise)", "Enterprise Support"]),
    "ComputeSavingsPlans": ("Compute", ["Savings Plans for AWS Compute usage", "Compute Savings Plans"]),
    "MachineLearningSavingsPlans": ("AI + Machine Learning", ["Savings Plans for AWS Machine Learning",
                                                              "Machine Learning Savings Plans",
                                                              "SageMaker Savings Plans"]),
}

# GCP: first keyword hit wins (checked against the normalized display name)
GCP_KEYWORD_CATEGORY = [
    ("kubernetes", "Containers"), ("gke", "Containers"), ("artifact registry", "Containers"),
    ("compute engine", "Compute"), ("cloud run", "Compute"), ("cloud functions", "Compute"),
    ("app engine", "Compute"), ("vmware", "Compute"),
    ("bigquery", "Analytics"), ("dataflow", "Analytics"), ("dataproc", "Analytics"),
    ("pub sub", "Integration"), ("pubsub", "Integration"),
    ("cloud sql", "Databases"), ("spanner", "Databases"), ("firestore", "Databases"),
    ("bigtable", "Databases"), ("memorystore", "Databases"), ("alloydb", "Databases"),
    ("cloud storage", "Storage"), ("filestore", "Storage"), ("backup", "Storage"),
    ("vertex", "AI + Machine Learning"), ("gemini", "AI + Machine Learning"),
    ("vision", "AI + Machine Learning"), ("speech", "AI + Machine Learning"),
    ("networking", "Networking"), ("load balanc", "Networking"), ("cdn", "Networking"),
    ("dns", "Networking"), ("vpn", "Networking"), ("interconnect", "Networking"),
    ("logging", "Management"), ("monitoring", "Management"),
    ("key management", "Security"), ("secret manager", "Security"), ("security", "Security"),
    ("support", "Support"),
]


# Microsoft invoices name a charge "<service>-<SKU>-<unit>" in short words ("Az DDOS Prot-NW Prot Plan-1 Hr"). The
# resolver expands the words of the part before the first dash and looks that up (normalize.azure_head).
AZURE_ABBREVIATIONS = {
    "az": "azure", "prem": "premium", "std": "standard", "bsc": "basic", "mngd": "managed", "mgd": "managed",
    "dsks": "disks", "dsk": "disk", "prot": "protection", "mgmt": "management", "svc": "service", "svcs": "services",
    "nw": "network", "pvt": "private", "rtn": "routing", "pref": "preference", "sgl": "single", "el": "elastic",
    "pur": "purpose", "gen": "general", "contnr": "container", "reg": "registry", "bndwdth": "bandwidth",
    "rgn": "region", "blk": "block", "ins": "insight", "rec": "recovery", "gw": "gateway", "p/l": "private link",
    "db": "database", "azsqldb": "sql database", "sqldb": "sql database", "vms": "virtual machines",
    "vm": "virtual machines", "sing": "single", "gp": "general purpose", "hyp": "hyperscale", "strg": "storage",
    "dl": "data lake", "gbb": "general block blob", "blb": "blob", "trxns": "transactions",
}

# Azure names the abbreviations do not reach, as the catalog's service names (the head after expansion)
AZURE_ALIASES = {
    "Storage": ["Managed Disks", "Premium SSD Managed Disks", "Standard HDD Managed Disks",
                "Standard SSD Managed Disks", "Files", "Files v2", "File Sync", "Blob Features", "Tiered Block Blob",
                "Premium Page Blob", "Standard Page Blob", "Standard Page Blob v2", "General Block Blob",
                "Blob Storage", "Page Blob", "Block Blob", "Data Lake Storage", "General Block Blob v2"],
    "SQL Database": ["SQL Database Single", "SQL Database Single Standard", "SQL Database Single Elastic"],
    "Application Gateway": ["WAF App Gateway", "Basic App Gateway", "App Gateway"],
    "Azure Cognitive Search": ["AI Search"],
    "Bandwidth": ["Routing Preference", "Bandwidth Inter Region"],
    "Virtual Network": ["IP Address", "Virtual Network Peering", "Virtual Network Private Link", "Private Link"],
    "Virtual Machines Licenses": ["RHEL"],
    "Azure DevOps": ["Repos and Boards"],
    "Azure Synapse Analytics": ["Synapse"],
    "Azure App Service": ["App Service"],
    "Azure Cosmos DB": ["Cosmos Database"],
}


# Third-party products sold through a cloud's Marketplace, by the seller or product name a bill prints. They are the
# names of the provider's "Marketplace" service (category Marketplace). Add more on the Mapping page: an alias to the
# code "Marketplace" (it matches as a word prefix, so "Veeam" covers every Veeam product).
MARKETPLACE_SELLERS = {
    "azure": ["Veeam", "SendGrid", "Twilio SendGrid"],
    "aws": [],
    "gcp": [],
}


# Services whose charges are fees (charge type "Fee"), not usage, wherever they are billed
FEE_SERVICES = {"AWSPartnerNetwork"}
