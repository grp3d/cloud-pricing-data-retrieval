# Backend for infra/bootstrap (account singletons). Set `bucket` to the state bucket name you
# chose for the bootstrap stack (its `state_bucket_name` variable).
bucket       = "cloud-pricing-shared-tfstate-g08a9i"
key          = "shared/bootstrap.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true
