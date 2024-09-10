import boto3
import kubernetes
from kubernetes.client import V1ObjectMeta, V1Ingress, V1IngressSpec, V1IngressRule, V1HTTPIngressPath, V1HTTPIngressRuleValue, V1IngressBackend, V1ServiceBackendPort
from kubernetes.client.rest import ApiException
from botocore.exceptions import ClientError
import os

# AWS Clients initialized with IRSA permissions
ec2_client = boto3.client('ec2')
elbv2_client = boto3.client('elbv2')

# Kubernetes client initialized using the in-cluster configuration
kubernetes.config.load_incluster_config()
v1 = kubernetes.client.NetworkingV1Api()

NAMESPACE = "system-application"
TARGET_NAMESPACE = "kube-system"

def get_load_balancers_by_tag(key, value_prefix):
    """Retrieve ALBs based on specific tag key and a value starting with the given prefix."""
    try:
        response = elbv2_client.describe_load_balancers()
        load_balancers = response['LoadBalancers']

        # Filter ALBs based on tag
        filtered_albs = []
        for alb in load_balancers:
            alb_name = alb['LoadBalancerName']
            if alb_name.startswith('shared-external-alb') or alb_name.startswith('shared-internal-alb'):
                alb_arn = alb['LoadBalancerArn']
                tags = elbv2_client.describe_tags(ResourceArns=[alb_arn])['TagDescriptions'][0]['Tags']
                alb_tags = {tag['Key']: tag['Value'] for tag in tags}
                
                # Ensure the value of 'ingress.k8s.aws/stack' starts with the correct prefix
                stack_tag_value = alb_tags.get(key, '')
                if stack_tag_value.startswith(value_prefix):
                    alb['Tags'] = alb_tags  # Attach tags to ALB for later use
                    filtered_albs.append(alb)
        return filtered_albs
    except ClientError as e:
        print(f"Error retrieving ALBs by tag {key} with value starting with {value_prefix}: {e}")
        return []

def get_security_groups_for_albs(albs):
    """Retrieve the security groups associated with the ALBs."""
    sg_ids = []
    for alb in albs:
        sg_ids.extend(alb['SecurityGroups'])
    return sg_ids

def create_ingress_object(alb, security_groups, is_external):
    """Create an Ingress object based on the ALB type."""
    alb_name = alb['LoadBalancerName']
    ingress_name = f"system-{alb_name}-ingress"
    
    # Get the value of 'ingress.k8s.aws/stack' for group.name
    group_name = alb['Tags'].get('ingress.k8s.aws/stack', 'default')

    ingress_annotations = {
        "alb.ingress.kubernetes.io/load-balancer-name": alb_name,
        "alb.ingress.kubernetes.io/security-groups": ','.join(security_groups),
        "alb.ingress.kubernetes.io/scheme": "internet" if is_external else "internal",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        "alb.ingress.kubernetes.io/target-type": "ip",
        "alb.ingress.kubernetes.io/group.name": group_name,  # Correctly assign value from ALB tag
        "alb.ingress.kubernetes.io/group.order": "-1000"
    }

    # Customizing annotations based on ALB type
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
                                port=V1ServiceBackendPort(name="use-annotation")  # Use port from annotation
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
            namespace=TARGET_NAMESPACE,  # Ensure it's created in kube-system namespace
            annotations=ingress_annotations
        ),
        spec=ingress_spec
    )
    return ingress

def apply_ingress(ingress):
    """Apply the Ingress object to the Kubernetes cluster."""
    try:
        v1.create_namespaced_ingress(namespace=TARGET_NAMESPACE, body=ingress)
        print(f"Ingress {ingress.metadata.name} created successfully in {TARGET_NAMESPACE}.")
    except ApiException as e:
        if e.status == 409:
            print(f"Ingress {ingress.metadata.name} already exists.")
        else:
            print(f"Error creating ingress {ingress.metadata.name}: {e}")

def main():
    # Get External ALBs with the correct tag (shared-external-*)
    external_albs = get_load_balancers_by_tag('ingress.k8s.aws/stack', 'shared-external-')
    external_sgs = get_security_groups_for_albs(external_albs)

    # Get Internal ALBs with the correct tag (shared-internal-*)
    internal_albs = get_load_balancers_by_tag('ingress.k8s.aws/stack', 'shared-internal-')
    internal_sgs = get_security_groups_for_albs(internal_albs)

    # Apply Ingresses for External ALBs
    for alb in external_albs:
        ingress = create_ingress_object(alb, external_sgs, is_external=True)
        apply_ingress(ingress)

    # Apply Ingresses for Internal ALBs
    for alb in internal_albs:
        ingress = create_ingress_object(alb, internal_sgs, is_external=False)
        apply_ingress(ingress)

if __name__ == "__main__":
    main()
