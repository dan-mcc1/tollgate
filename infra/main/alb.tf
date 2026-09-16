# The front door: HTTPS on 443, a redirect on 80, and a target group that only sends
# traffic to tasks whose /readyz returns 200.

resource "aws_lb" "main" {
  name               = var.name
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  # Seconds with no bytes flowing before the ALB closes the connection. A non-streaming
  # model call sends nothing until it finishes (we've measured 78 s), so the default of 60
  # is far too tight. This is the last resort only: the gateway's own 120 s deadline fires
  # first and returns an error that explains itself. Streaming responses keep sending, so
  # they are governed by the upstream read timeout, not by this.
  idle_timeout = 150

  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name        = var.name
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # Fargate tasks are registered by IP, not by instance

  # How long a task being replaced keeps finishing in-flight requests. The default is
  # 300 s, which makes every deploy wait five minutes for nothing.
  deregistration_delay = 30

  health_check {
    path                = "/readyz"
    matcher             = "200"
    interval            = 15
    timeout             = 5 # /readyz's own checks time out at 2 s and 3 s, in parallel
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06" # TLS 1.2 and 1.3 only
  certificate_arn   = aws_acm_certificate_validation.tollgate.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}
