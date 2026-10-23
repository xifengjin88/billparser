"""Hand-maintained seed data that the public catalogs don't provide.

- A shared category list, so AWS, Azure and GCP roll up the same way.
- AWS categories and common aliases (the AWS price index has codes, not categories).
- Aliases seen on invoices and Cost Explorer exports ("EC2-Other", "EC2 - Instances").
- Keyword rules to categorize GCP services.

Edit freely. Anything learned from corrections goes in data/synonyms.json instead.
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
}

# AWS service code -> (category, extra aliases). Names come from the fetch script.
AWS_SEED = {
    "AmazonEC2": ("Compute", ["EC2", "EC2-Instances", "EC2 - Instances", "EC2-Other", "EC2 - Other",
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
    "AmazonOpenSearchService": ("Analytics", ["OpenSearch", "Elasticsearch Service", "Amazon Elasticsearch Service"]),
    "AmazonAthena": ("Analytics", ["Athena"]),
    "AWSGlue": ("Analytics", ["Glue", "AWS Glue"]),
    "ElasticMapReduce": ("Analytics", ["EMR", "Elastic MapReduce"]),
    "AmazonKinesis": ("Analytics", ["Kinesis"]),
    "AmazonMSK": ("Analytics", ["MSK", "Managed Streaming for Apache Kafka"]),
    "AmazonQuickSight": ("Analytics", ["QuickSight"]),
    "AmazonSageMaker": ("AI + Machine Learning", ["SageMaker"]),
    "AmazonBedrock": ("AI + Machine Learning", ["Bedrock"]),
    "AmazonVPC": ("Networking", ["VPC", "Virtual Private Cloud", "NAT Gateway"]),
    "AmazonCloudFront": ("Networking", ["CloudFront"]),
    "AmazonRoute53": ("Networking", ["Route 53", "Route53"]),
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
    "awswaf": ("Security", ["WAF", "AWS WAF"]),
    "AWSSecurityHub": ("Security", ["Security Hub"]),
    "AmazonSNS": ("Integration", ["SNS", "Simple Notification Service"]),
    "AWSQueueService": ("Integration", ["SQS", "Simple Queue Service"]),
    "AWSEvents": ("Integration", ["EventBridge", "CloudWatch Events"]),
    "AWSStepFunctions": ("Integration", ["Step Functions"]),
    "AmazonSES": ("Integration", ["SES", "Simple Email Service"]),
    "AWSSupportBusiness": ("Support", ["AWS Support (Business)", "Business Support"]),
    "AWSSupportEnterprise": ("Support", ["AWS Support (Enterprise)", "Enterprise Support"]),
    "ComputeSavingsPlans": ("Compute", ["Savings Plans for AWS Compute usage", "Compute Savings Plans"]),
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
