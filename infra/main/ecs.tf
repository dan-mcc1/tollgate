# The cluster, the recipe (task definition), and the manager that keeps it running (service).

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "main" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "disabled" # extra per-metric charges; phase 5 brings its own metrics
  }
}

locals {
  initial_image = "${data.aws_ecr_repository.tollgate.repository_url}@${data.aws_ecr_image.initial.image_digest}"
}

resource "aws_ecs_task_definition" "app" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc" # each task gets its own network interface and IP
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # When Terraform replaces this resource, leave the old revision registered. The pipeline
  # rolls back by pointing the service at an earlier revision, and ECS refuses to switch a
  # service to one that has been deregistered.
  skip_destroy = true

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # matches the image built on your PC and on GitHub
  }

  container_definitions = jsonencode([{
    name      = "app"
    image     = local.initial_image
    essential = true

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    # Plain configuration. Visible to anyone who can read the task definition, so nothing
    # sensitive goes here.
    environment = [
      { name = "UPSTREAM_BASE_URL", value = var.upstream_base_url },
      { name = "GEMINI_MODEL", value = var.gemini_model },
      { name = "MOCK_GEMINI", value = "false" },
      # Rate-limit buckets live in Redis, not in the task. With more than one task the
      # in-process limiter would give each tenant its limit once per task.
      { name = "LIMITER_BACKEND", value = var.limiter_backend },
      # The pipeline overwrites this with the commit SHA on every deploy.
      { name = "APP_VERSION", value = try(data.aws_ecr_image.initial.image_tags[0], "unknown") },
    ]

    # Secrets: the task definition holds only the secret's ARN. ECS fetches the value with
    # the execution role at startup and injects it as an environment variable.
    secrets = [
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      { name = "GEMINI_API_KEY", valueFrom = data.aws_secretsmanager_secret.gemini_api_key.arn },
      { name = "REDIS_URL", valueFrom = data.aws_secretsmanager_secret.redis_url.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "app"
      }
    }

    # Liveness, checked by ECS inside the container. Separate from the load balancer's
    # readiness check: this one only asks "is the process answering at all?"
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3).status == 200 else 1)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }

    readonlyRootFilesystem = true                          # tested locally: the app never writes to disk
    linuxParameters        = { initProcessEnabled = true } # forwards stop signals, reaps zombies
    stopTimeout            = 30
  }])
}

resource "aws_ecs_service" "app" {
  name            = var.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # outbound internet without a NAT gateway; inbound is still ALB-only
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }

  # Rolling deploy: start the new task, wait for it to pass health checks, then stop the old.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # If new tasks keep failing to become healthy, ECS stops the deploy and puts the last
  # working task definition back by itself.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Don't count failed load balancer health checks until the app has had time to start.
  health_check_grace_period_seconds = 30

  # `terraform apply` doesn't finish until the service is actually healthy, so a broken
  # first deploy fails loudly here instead of looking like success.
  wait_for_steady_state = true

  lifecycle {
    # After creation, the deploy pipeline registers new task definition revisions and
    # points the service at them. Without this, the next `terraform apply` would quietly
    # roll the service back to whatever revision Terraform last created.
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.https]
}
