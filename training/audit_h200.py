"""Bounded audit host. Push with: truss train push training/audit_h200.py."""
from truss_train import TrainingProject, TrainingJob, Image, Compute, Runtime
from truss_train.definitions import CacheConfig, InteractiveSession, InteractiveSessionTrigger, InteractiveSessionProvider, InteractiveSessionAuthProvider
from truss.base.truss_config import AcceleratorSpec
training_job = TrainingJob(
    name='volume-audit-h200',
    image=Image(base_image='python:3.12-slim'),
    compute=Compute(accelerator=AcceleratorSpec(accelerator='H200',count=1)),
    runtime=Runtime(start_commands=['sleep 7200'],cache_config=CacheConfig(enabled=True)),
    interactive_session=InteractiveSession(trigger=InteractiveSessionTrigger.ON_STARTUP,session_provider=InteractiveSessionProvider.SSH,auth_provider=InteractiveSessionAuthProvider.GITHUB,timeout_minutes=120),
)
training_project=TrainingProject(name='volume-audit-h200',job=training_job)
