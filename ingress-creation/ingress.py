import boto3
from kubernetes import client, config
import re

def get_albs():
    elb_client = boto3.client('elbv2')
    response = elb_client.describe_load_balancers()
    return response['LoadBalancers']

def get_alb_tags(alb_arn):
    elb_client = boto3.client('elbv2')
    response = elb_client.describe_tags(ResourceArns=[alb_arn])
    return {tag['Key']: tag['Value'] for tag in response['TagDescriptions'][0]['Tags']}

def get_security_groups_by_alb_tag(tag_value):
    elb_client = boto3.client('elbv2')
    response = elb_client.describe_load_balancers()
    security_group_ids = []
    
    for lb in response['LoadBalancers']:
        lb_arn = lb['LoadBalancerArn']
        tags = get_alb_tags(lb_arn)
        
        # Check if the ALB has the tag "ingress.k8s.aws/stack" with the required value
        if tags.get('ingress.k8s.aws/stack') == tag_value:
            # Add the SecurityGroups associated with this ALB to the list
            if 'SecurityGroups' in lb:
                security_group_ids.extend(lb['SecurityGroups'])
    
    return security_group_ids

def extract_guid_from_alb_name(alb_name):
    match = re.search(r'-(\w+)$', alb_name)  # Extracting the GUID at the end
    return match.group(1) if match else None

def get_existing_ingresses():
    k8s_client = client.NetworkingV1Api()
    ingresses = k8s_client.list_namespaced_ingress(namespace="kube-system")
    existing_ingresses = {ing.metadata.name for ing in ingresses.items}
    return existing_ingresses

def create_ingress_object_with_annotations(alb, security_group_id):
    lb_name = alb['LoadBalancerName']
    lb_arn = alb['LoadBalancerArn']
    lb_scheme = alb['Scheme']
    
    # Retrieve tags for the ALB
    tags = get_alb_tags(lb_arn)
    group_name = tags.get('ingress.k8s.aws/stack', lb_name)

    annotations = {
        "alb.ingress.kubernetes.io/actions.healthcheck-v2": '{"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"HEALTH"}}',
        "alb.ingress.kubernetes.io/group.name": group_name,
        "alb.ingress.kubernetes.io/group.order": "-1000",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        "alb.ingress.kubernetes.io/load-balancer-name": lb_name,
        "alb.ingress.kubernetes.io/manage-backend-security-group": "true",
        "alb.ingress.kubernetes.io/scheme": "internet" if "external" in lb_name else "internal",
        "alb.ingress.kubernetes.io/target-type": "ip"
    }

    if security_group_id:
        annotations["alb.ingress.kubernetes.io/security-groups"] = security_group_id

    ingress_name = lb_name

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

def sync_albs_to_ingresses():
    config.load_incluster_config()
    k8s_client = client.NetworkingV1Api()
    
    albs = get_albs()
    existing_ingresses = get_existing_ingresses()

    for alb in albs:
        lb_name = alb['LoadBalancerName']
        tags = get_alb_tags(alb['LoadBalancerArn'])

        if lb_name.startswith('shared-external-alb') and tags.get('ingress.k8s.aws/stack', '').startswith('shared-external-'):
            ingress_name = lb_name
            security_group_ids = get_security_groups_by_alb_tag('shared-external')
        elif lb_name.startswith('shared-internal-alb') and tags.get('ingress.k8s.aws/stack', '').startswith('shared-internal-'):
            ingress_name = lb_name
            security_group_ids = get_security_groups_by_alb_tag('shared-internal')
        else:
            continue  # Skip ALBs that don't match the criteria

        # Use the first security group from the retrieved list (if available)
        security_group_id = security_group_ids[0] if security_group_ids else None

        if ingress_name not in existing_ingresses:
            ingress_object = create_ingress_object_with_annotations(alb, security_group_id)
            k8s_client.create_namespaced_ingress(namespace='kube-system', body=ingress_object)
            print(f"Created Ingress: {ingress_name}")
        else:
            print(f"Ingress {ingress_name} already exists.")

if __name__ == "__main__":
    sync_albs_to_ingresses()
