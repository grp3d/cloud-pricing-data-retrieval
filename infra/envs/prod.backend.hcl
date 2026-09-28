# Backend for the prod data and pipeline stacks. Pass the stack's key separately:
#   tofu init -backend-config=../envs/prod.backend.hcl -backend-config="key=prod/<stack>.tfstate"
bucket       = "cloud-pricing-shared-tfstate-g08a9i"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
