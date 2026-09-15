# HTTPS certificate and the public name, both inside the Route 53 zone bootstrap created.

resource "aws_acm_certificate" "tollgate" {
  domain_name       = var.domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true # a replacement cert is issued before the old one goes
  }
}

# ACM proves we control the domain by asking for a specific DNS record to exist.
resource "aws_route53_record" "certificate_validation" {
  for_each = {
    for option in aws_acm_certificate.tollgate.domain_validation_options :
    option.domain_name => option
  }

  zone_id         = data.aws_route53_zone.tollgate.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 60
  allow_overwrite = true # the record is identical across rebuilds; take it over if it lingers
}

# Waits until ACM has seen the record and issued the certificate (usually 1-5 minutes).
resource "aws_acm_certificate_validation" "tollgate" {
  certificate_arn         = aws_acm_certificate.tollgate.arn
  validation_record_fqdns = [for record in aws_route53_record.certificate_validation : record.fqdn]
}

# tollgate.danmccabe.dev -> the load balancer. An alias record follows the ALB's own
# addresses as they change, which a plain CNAME at a zone apex can't do.
resource "aws_route53_record" "app" {
  zone_id = data.aws_route53_zone.tollgate.zone_id
  name    = var.domain
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
