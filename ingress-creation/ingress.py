import boto3
import kubernetes
import os
from kubernetes.client import V1ObjectMeta, V1Ingress, V1IngressSpec, V1IngressRule, V1HTTPIngressPath, V1HTTPIngressRuleValue, V1IngressBackend, V1TypedLocalObjectReference
from kubernetes.client.rest import ApiException
from botocore.exceptions import ClientError

# Initialize clients
ec2_client = boto3.client('ec2')
elbv2_client = boto3.client('elbv2')
k8s_client = kubernetes.config.load_incluster_config()

# Kubernetes API client
v1 = kubernetes.client.NetworkingV1Api()

NAMESPACE = "kube-system"

def get_load_balancers_by_tag(key, value):
    """Retrieve ALBs with a specific tag key and value"""
    try:
        response = elbv2_client.describe_load_balancers()
        load_balancers = response['LoadBalancers']

        # Filter ALBs based on the given tag key-value pair
        filtered_albs = []
        for alb in load_balancers:
            alb_arn = alb['LoadBalancerArn']
            tags = elbv2_client.describe_tags(ResourceArns=[alb_arn])['TagDescriptions'][0]['Tags']
            for tag in tags:
                if tag['Key'] == key and tag['Value'] == value:
                    filtered_albs.append(alb)
        return filtered_albs
    except ClientError as e:
        print(f"Error retrieving ALBs by tag {key}={value}: {e}")
        return []

def get_security_groups_for_albs(albs):
    """Retrieve security groups associated with the ALBs"""
    sg_ids = []
    for alb in albs:
        sg_ids.extend(alb['SecurityGroups'])
    return sg_ids

def create_ingress_object(alb, security_groups, is_external):
    """Create an Ingress object based on the ALB type (external/internal)"""
    alb_name = alb['LoadBalancerName']
    ingress_name = f"system-{alb_name}-ingress"
    ingress_annotations = {
        "alb.ingress.kubernetes.io/load-balancer-name": alb_name,
        "alb.ingress.kubernetes.io/security-groups": ','.join(security_groups),
        "alb.ingress.kubernetes.io/scheme": "internet" if is_external else "internal",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        "alb.ingress.kubernetes.io/target-type": "ip",
        "alb.ingress.kubernetes.io/group.name": alb_name,
        "alb.ingress.kubernetes.io/group.order": "-1000"
    }

    # Customizing annotation based on external/internal ALB
    if is_external:
        ingress_annotations["alb.ingress.kubernetes.io/actions.healthcheck-v2"] = (
            '{"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"HEALTHY"}}'
        )
        ingress_path = "/healthcheck"
    else:
        ingress_annotations["alb.ingress.kubernetes.io/actions.listener-protection-v2"] = (
            '{"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"Secure Listener Protection"}}'
        )
        ingress_path = "/sys-internal"

    # Define the Ingress spec
    ingress_spec = V1IngressSpec(
        rules=[
            V1IngressRule(
                http=V1HTTPIngressRuleValue(
                    paths=[
                        V1HTTPIngressPath(
                            path=ingress_path,
                            path_type="Exact",
                            backend=V1IngressBackend(
                                service=V1TypedLocalObjectReference(
                                    name="healthcheck-v2",
                                    kind="Service",
                                ),
                            )
                        )
                    ]
                )
            )
        ]
    )

    # Create the Ingress object
    ingress = V1Ingress(
        metadata=V1ObjectMeta(
            name=ingress_name,
            namespace=NAMESPACE,
            annotations=ingress_annotations
        ),
        spec=ingress_spec
    )
    return ingress

def apply_ingress(ingress):
    """Apply the Ingress object to the Kubernetes cluster"""
    try:
        v1.create_namespaced_ingress(namespace=NAMESPACE, body=ingress)
        print(f"Ingress {ingress.metadata.name} created successfully.")
    except ApiException as e:
        if e.status == 409:
            print(f"Ingress {ingress.metadata.name} already exists.")
        else:
            print(f"Error creating ingress {ingress.metadata.name}: {e}")

def main():
    # Retrieve external ALBs
    external_albs = get_load_balancers_by_tag('ingress.k8s.aws/stack', 'shared-external')
    external_sgs = get_security_groups_for_albs(external_albs)

    # Retrieve internal ALBs
    internal_albs = get_load_balancers_by_tag('ingress.k8s.aws/stack', 'shared-internal')
    internal_sgs = get_security_groups_for_albs(internal_albs)

    # Apply Ingresses for external ALBs
    for alb in external_albs:
        ingress = create_ingress_object(alb, external_sgs, is_external=True)
        apply_ingress(ingress)

    # Apply Ingresses for internal ALBs
    for alb in internal_albs:
        ingress = create_ingress_object(alb, internal_sgs, is_external=False)
        apply_ingress(ingress)

if __name__ == "__main__":
    main()
