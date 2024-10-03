import boto3
from kubernetes import client, config

def get_albs():
    elb_client = boto3.client('elbv2')
    response = elb_client.describe_load_balancers()
    return response['LoadBalancers']

def get_alb_tags(alb_arn):
    elb_client = boto3.client('elbv2')
    response = elb_client.describe_tags(ResourceArns=[alb_arn])
    return {tag['Key']: tag['Value'] for tag in response['TagDescriptions'][0]['Tags']}

def get_security_groups_for_alb(alb):
    # Retrieve the security groups directly from the current ALB
    if 'SecurityGroups' in alb:
        return alb['SecurityGroups']
    return []

def get_existing_ingresses():
    k8s_client = client.NetworkingV1Api()
    ingresses = k8s_client.list_namespaced_ingress(namespace="kube-system")
    existing_ingresses = {ing.metadata.name for ing in ingresses.items}
    return existing_ingresses

def create_ingress_object_with_annotations(alb, security_group_ids):
    lb_name = alb['LoadBalancerName']
    lb_arn = alb['LoadBalancerArn']
    lb_scheme = alb['Scheme']
    
    # Retrieve tags for the ALB
    tags = get_alb_tags(lb_arn)
    group_name = tags.get('ingress.k8s.aws/stack', lb_name)

    # Combine all associated security groups into a comma-separated string
    security_group_ids_str = ",".join(security_group_ids) if security_group_ids else None

    # Determine if the ALB is external or internal based on its scheme or name
    if "external" in lb_name:
        annotations = {
            "alb.ingress.kubernetes.io/actions.healthcheck-v2": '''|
  {"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"HEALTHY"}}''',
            "alb.ingress.kubernetes.io/group.name": group_name,
            "alb.ingress.kubernetes.io/group.order": "-1000",
            "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
            "alb.ingress.kubernetes.io/load-balancer-name": lb_name,
            "alb.ingress.kubernetes.io/manage-backend-security-group": "true",
            "alb.ingress.kubernetes.io/scheme": "internet-facing",
            "alb.ingress.kubernetes.io/target-type": "ip"
        }
        ingress_path = "/healthcheck"
    else:
        annotations = {
            "alb.ingress.kubernetes.io/actions.listener-protection-v2": '''|
  {"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"Secure Listener Protection"}}''',
            "alb.ingress.kubernetes.io/group.name": group_name,
            "alb.ingress.kubernetes.io/group.order": "-1000",
            "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
            "alb.ingress.kubernetes.io/load-balancer-name": lb_name,
            "alb.ingress.kubernetes.io/manage-backend-security-group": "true",
            "alb.ingress.kubernetes.io/scheme": "internal",
            "alb.ingress.kubernetes.io/target-type": "ip"
        }
        ingress_path = "/sys-internal"

    # Add the security group annotation if security groups are available
    if security_group_ids_str:
        annotations["alb.ingress.kubernetes.io/security-groups"] = security_group_ids_str

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
                                path=ingress_path,
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

        # Ensure that we only handle ALBs with names starting with shared-external or shared-internal
        if lb_name.startswith('shared-external-alb') and tags.get('ingress.k8s.aws/stack', '').startswith('shared-external-'):
            ingress_name = lb_name
            security_group_ids = get_security_groups_for_alb(alb)
        elif lb_name.startswith('shared-internal-alb') and tags.get('ingress.k8s.aws/stack', '').startswith('shared-internal-'):
            ingress_name = lb_name
            security_group_ids = get_security_groups_for_alb(alb)
        else:
            continue  # Skip ALBs that don't match the criteria

        # Check if Ingress already exists
        if ingress_name not in existing_ingresses:
            ingress_object = create_ingress_object_with_annotations(alb, security_group_ids)
            k8s_client.create_namespaced_ingress(namespace='kube-system', body=ingress_object)
            print(f"Created Ingress: {ingress_name}")
        else:
            print(f"Ingress {ingress_name} already exists.")

if __name__ == "__main__":
    sync_albs_to_ingresses()
