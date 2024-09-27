import os
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
        
        if tags.get('ingress.k8s.aws/stack') == tag_value:
            if 'SecurityGroups' in lb:
                security_group_ids.extend(lb['SecurityGroups'])
    
    return security_group_ids

def get_existing_ingresses():
    k8s_client = client.NetworkingV1Api()
    ingresses = k8s_client.list_namespaced_ingress(namespace="kube-system")
    existing_ingresses = {ing.metadata.name for ing in ingresses.items}
    return existing_ingresses

def create_ingress_object_with_annotations(alb, security_group_ids, is_external):
    lb_name = alb['LoadBalancerName']
    lb_arn = alb['LoadBalancerArn']
    lb_scheme = alb['Scheme']
    tags = get_alb_tags(lb_arn)
    group_name = tags.get('ingress.k8s.aws/stack', lb_name)
    security_group_ids_str = ",".join(security_group_ids) if security_group_ids else None

    annotations = {
        "alb.ingress.kubernetes.io/group.name": group_name,
        "alb.ingress.kubernetes.io/group.order": "-1000",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        "alb.ingress.kubernetes.io/load-balancer-name": lb_name,
        "alb.ingress.kubernetes.io/manage-backend-security-group": "true",
        "alb.ingress.kubernetes.io/scheme": "internet" if is_external else "internal",
        "alb.ingress.kubernetes.io/target-type": "ip"
    }

    if security_group_ids_str:
        annotations["alb.ingress.kubernetes.io/security-groups"] = security_group_ids_str

    if is_external:
        annotations["alb.ingress.kubernetes.io/actions.healthcheck-v2"] = '''|
  {"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"HEALTHY"}}'''
        path = "/healthcheck"
    else:
        annotations["alb.ingress.kubernetes.io/actions.listener-protection-v2"] = '''|
  {"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"Secure Listener Protection"}}'''
        path = "/sys-internal"

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
                                path=path,
                                path_type="Exact",
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name="healthcheck-v2" if is_external else "listener-protection-v2",
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
    
    cluster_name = os.getenv('CLUSTER', None)
    if not cluster_name:
        print("CLUSTER environment variable is not set.")
        return

    albs = get_albs()
    existing_ingresses = get_existing_ingresses()

    for alb in albs:
        lb_name = alb['LoadBalancerName']
        lb_arn = alb['LoadBalancerArn']
        tags = get_alb_tags(lb_arn)

        # Check if the ALB has the elbv2.k8s.aws/cluster tag and if it matches the CLUSTER environment variable
        if tags.get('elbv2.k8s.aws/cluster') != cluster_name:
            print(f"Skipping ALB {lb_name}: cluster tag does not match the CLUSTER environment variable.")
            continue

        # Process ALBs with shared-external- or shared-internal- prefix for ingress creation
        if lb_name.startswith('shared-external-alb') and tags.get('ingress.k8s.aws/stack', '').startswith('shared-external-'):
            security_group_ids = get_security_groups_by_alb_tag('shared-external')
            is_external = True
        elif lb_name.startswith('shared-internal-alb') and tags.get('ingress.k8s.aws/stack', '').startswith('shared-internal-'):
            security_group_ids = get_security_groups_by_alb_tag('shared-internal')
            is_external = False
        else:
            continue

        ingress_name = lb_name

        if ingress_name not in existing_ingresses:
            ingress_object = create_ingress_object_with_annotations(alb, security_group_ids, is_external)
            k8s_client.create_namespaced_ingress(namespace='kube-system', body=ingress_object)
            print(f"Created Ingress: {ingress_name} for ALB: {lb_name}")
        else:
            print(f"Ingress {ingress_name} already exists.")

if __name__ == "__main__":
    sync_albs_to_ingresses()
