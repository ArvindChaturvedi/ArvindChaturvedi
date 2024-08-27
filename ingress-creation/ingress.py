import boto3
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import random

# Initialize the Boto3 client for ELBv2 (ALBs)
elb_client = boto3.client('elbv2')
sg_client = boto3.client('ec2')

# Load Kubernetes config (assuming the script runs within the cluster)
config.load_incluster_config()

# Initialize Kubernetes API client
k8s_client = client.NetworkingV1Api()

def get_all_albs_with_tags():
    paginator = elb_client.get_paginator('describe_load_balancers')
    albs_with_tags = []
    for page in paginator.paginate():
        for lb in page['LoadBalancers']:
            if lb['Type'] == 'application':
                lb_arn = lb['LoadBalancerArn']
                tags = elb_client.describe_tags(ResourceArns=[lb_arn])
                tag_dict = {tag['Key']: tag['Value'] for tag in tags['TagDescriptions'][0]['Tags']}
                lb['Tags'] = tag_dict
                albs_with_tags.append(lb)
    return albs_with_tags

def get_ingress_name(scheme):
    return f"system-{scheme}-ingress-{random.randint(1000, 9999)}"

def determine_scheme(lb_name):
    return "internet" if "external" in lb_name.lower() else "internal"

def find_security_group(scheme):
    filters = [
        {'Name': 'group-name', 'Values': [f"{scheme}*"]},
        {'Name': 'description', 'Values': ['managed by terraform']}
    ]
    sgs = sg_client.describe_security_groups(Filters=filters)
    if sgs['SecurityGroups']:
        return sgs['SecurityGroups'][0]['GroupId']
    return None

def create_ingress_object_with_annotations(alb):
    scheme = determine_scheme(alb['LoadBalancerName'])
    security_group = find_security_group(scheme)
    
    ingress_name = get_ingress_name(scheme)
    alb_tags = alb['Tags']
    alb_group_name = alb_tags.get('ingress.k8s.aws/stack', 'default-stack')

    annotations = {
        "alb.ingress.kubernetes.io/actions.healthcheck-v2": '{"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"HEALTH"}}',
        "alb.ingress.kubernetes.io/group.name": alb_group_name,
        "alb.ingress.kubernetes.io/group.order": "-1000",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        "alb.ingress.kubernetes.io/load-balancer-name": alb['LoadBalancerName'],
        "alb.ingress.kubernetes.io/manage-backend-security-group": "true",
        "alb.ingress.kubernetes.io/scheme": scheme,
        "alb.ingress.kubernetes.io/security-groups": security_group if security_group else "default-security-group",
        "alb.ingress.kubernetes.io/target-type": "ip",
    }

    body = client.V1Ingress(
        api_version="networking.k8s.io/v1",
        kind="Ingress",
        metadata=client.V1ObjectMeta(
            name=ingress_name,
            namespace='kube-system',
            annotations=annotations
        ),
        spec=client.V1IngressSpec(
            ingress_class_name="alb",
            rules=[
                client.V1IngressRule(
                    http=client.V1HTTPIngressRuleValue(
                        paths=[
                            client.V1HTTPIngressPath(
                                path="/healthcheck",
                                path_type="Exact",
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name="healthcheck-v2",
                                        port=client.V1ServiceBackendPort(name="use-annotation")
                                    )
                                )
                            )
                        ]
                    )
                )
            ]
        )
    )
    return body

def ensure_ingress_exists_with_annotations(alb):
    ingress_name = f"alb-{alb['LoadBalancerName']}"
    try:
        k8s_client.read_namespaced_ingress(name=ingress_name, namespace='kube-system')
        print(f"Ingress {ingress_name} already exists.")
    except ApiException as e:
        if e.status == 404:
            print(f"Creating ingress {ingress_name}...")
            body = create_ingress_object_with_annotations(alb)
            k8s_client.create_namespaced_ingress(namespace='kube-system', body=body)
        else:
            raise

def main():
    albs = get_all_albs_with_tags()
    for alb in albs:
        ensure_ingress_exists_with_annotations(alb)

if __name__ == '__main__':
    main()
