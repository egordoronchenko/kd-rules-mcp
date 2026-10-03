"""Чистое ядро авторинга прямых ПКС шапки EnterpriseData."""

from .candidates import Candidate as Candidate
from .candidates import candidates as candidates
from .canonical import canonicalize_operations as canonicalize_operations
from .hook import generate_hook as generate_hook
from .model import AddHeaderProperty as AddHeaderProperty
from .model import AttributeDraft as AttributeDraft
from .model import AuthoringInputs as AuthoringInputs
from .model import AuthoringPreconditionError as AuthoringPreconditionError
from .model import AuthoringTarget as AuthoringTarget
from .model import ExtensionIdentity as ExtensionIdentity
from .model import Failure as Failure
from .model import GeneratedHook as GeneratedHook
from .model import MetadataProfile as MetadataProfile
from .model import Notice as Notice
from .model import PreparedAuthoring as PreparedAuthoring
from .model import Projection as Projection
from .model import SourceSet as SourceSet
from .operations import apply_header_properties as apply_header_properties
from .operations import copy_structure_with_attributes as copy_structure_with_attributes
from .operations import validate_preconditions as validate_preconditions
