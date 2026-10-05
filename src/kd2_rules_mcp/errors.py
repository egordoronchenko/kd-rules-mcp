"""Типизированные ошибки сервера (сообщения — на русском)."""

from collections.abc import Sequence
from typing import Any


class Kd2Error(Exception):
    """Базовая ошибка сервера правил КД 2."""


class EdAuthoringPreconditionError(Kd2Error):
    """Authoring preconditions failed; see failures and summary."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class EdAuthoringAckRequiredError(EdAuthoringPreconditionError):
    """Specific notices need acknowledgement for the current preview_hash."""


class EdAuthoringStaleError(EdAuthoringPreconditionError):
    """Input files, preview decisions or the previous manifest changed."""


class EdAuthoringPathError(EdAuthoringPreconditionError):
    """Directory outside workspace, foreign content, symbolic link or junction."""


class EdAuthoringResourceLimitError(EdAuthoringPreconditionError):
    """Operation, manager, profile or kit file limit exceeded."""


class EdAuthoringIoError(EdAuthoringPreconditionError):
    """Read or atomic write failed; the previous kit is preserved."""


class EdSchemaNotFoundError(Kd2Error):
    """Format schema is not open."""


class EdSchemaTypeNotFoundError(Kd2Error):
    """Type is absent from the open schema."""


class EdSchemaReadError(Kd2Error):
    """XDTO package file is unavailable."""


class EdSchemaFormatError(Kd2Error):
    """Malformed XML or unsupported package format."""


class EdSchemaConflictError(Kd2Error):
    """One QName has conflicting definitions."""


class EdSchemaAmbiguousImportError(Kd2Error):
    """Several package descriptions share an import URI."""


class EdSchemaProfileMismatchError(Kd2Error):
    """Version, package description or extension disagrees with the base package."""


class EdSchemaResourceLimitError(Kd2Error):
    """Schema read or storage limit exceeded."""


class EdReadError(Kd2Error):
    """ED manager file is unavailable or has an unsupported encoding."""


class EdFormatError(Kd2Error):
    """Module is outside the safely readable ED manager subset."""


class EdResourceLimitError(Kd2Error):
    """ED manager size or line count limit exceeded."""


class EdRouteProfileNotFoundError(Kd2Error):
    """Route snapshot is unknown or evicted."""


class EdRouteReadError(Kd2Error):
    """Route dump path or root is unreadable."""


class EdRouteFormatError(Kd2Error):
    """Incomplete configuration XML dump or malformed Configuration.xml."""


class EdRouteResourceLimitError(Kd2Error):
    """Route dump read or snapshot storage limit exceeded."""


class RulesFormatError(Kd2Error):
    """Rules file cannot be parsed or is outside the KD 2 format."""


class StructureFormatError(Kd2Error):
    """Configuration structure cannot be parsed or is not the expected dump."""


class StructureNotFoundError(Kd2Error):
    """Configuration structure ID is absent from the cache."""


class ObjectNotFoundError(Kd2Error):
    """Metadata object is absent from the structure; suggestions lists similar names."""

    def __init__(self, message: str, suggestions: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.suggestions = list(suggestions)


class WorkspacePathError(Kd2Error):
    """Путь записи лежит вне рабочей папки и папок живых правил проектов (rules_dir)."""


class ProjectNotFoundError(Kd2Error):
    """Rules project or ED snapshot ID is not open."""


class DuplicateProjectError(Kd2Error):
    """Rules project ID is already in use."""


class RuleEditError(Kd2Error):
    """Rule edit was rejected."""


class UnknownFieldError(RuleEditError):
    """Field is outside the schema for this rule kind."""


class DuplicateRuleError(RuleEditError):
    """Code or name is already in use in its list."""


class RuleNotFoundError(RuleEditError):
    """No rule was found at the address."""


class AmbiguousAddressError(RuleNotFoundError):
    """Address matches several rules or entities; qualify it explicitly."""

    def __init__(self, message: str, candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.candidates = list(candidates)
        self.candidate_page: dict[str, Any] | None = None


class DanglingReferenceError(RuleEditError):
    """Reference to a missing rule, object, property or value."""


class RegistrationRetargetError(Kd2Error):
    """Retargeting registration rules to another exchange plan was rejected."""

    code = "registration.retarget"


class NotRegistrationRulesError(RegistrationRetargetError):
    """The document is not registration rules."""

    code = "registration.not_registration"


class InvalidRegistrationNameError(RegistrationRetargetError):
    """Empty or invalid exchange plan name or node property name."""

    code = "registration.invalid_name"


class DuplicateTargetPropertyError(RegistrationRetargetError):
    """Two different node properties are renamed to the same name."""

    code = "registration.duplicate_target"


class PropertyNameClashError(RegistrationRetargetError):
    """The new name is already used by another node property in these rules."""

    code = "registration.property_clash"


class RetargetInvariantError(RegistrationRetargetError):
    """The result changed places that retargeting must not change."""

    code = "registration.invariant"


class RegistrationToolError(Kd2Error):
    """Перенос регистрации отклонён; код и адреса объясняют, что исправить."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        code: str = "registration.precondition",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class RegistrationDeliveryError(Kd2Error):
    """Registration kit delivery preconditions were not met."""

    code = "registration.delivery"


class RegistrationAttributeClashError(RegistrationDeliveryError):
    """An own node attribute collides with existing metadata."""

    code = "registration.attribute_clash"


class RegistrationAttributePrefixError(RegistrationDeliveryError):
    """An own node attribute does not use the extension prefix."""

    code = "registration.attribute_prefix"


class RegistrationMissingAttributeError(RegistrationDeliveryError):
    """Rules reference an attribute absent from the delivered metadata."""

    code = "registration.missing_attribute"


class RegistrationDeliveryProfileError(RegistrationDeliveryError):
    """The input is outside the supported registration delivery profile."""

    code = "registration.delivery_profile"


class RegistrationPlanNotFoundError(RegistrationDeliveryError):
    """The target exchange plan was not found in the configuration dump."""

    code = "registration.plan_not_found"


class RegistrationExtensionClashError(RegistrationDeliveryError):
    """Another extension already owns a requested node attribute."""

    code = "registration.extension_clash"


class EdFormatPackageError(Kd2Error):
    """Format package authoring preconditions were not met."""


class EdFormatDuplicateNameError(EdFormatPackageError):
    """A local type or property name is repeated in its namespace."""


class EdFormatUnknownTypeError(EdFormatPackageError):
    """A QName does not resolve in the own package or explicit imports."""


class EdFormatIdentifierError(EdFormatPackageError):
    """A metadata identifier or XDTO local name is invalid."""


class EdFormatNamespaceError(EdFormatPackageError):
    """The own namespace conflicts with the base or a declaration."""


class EdFormatEmptyObjectError(EdFormatPackageError):
    """An authored object type has no properties."""


class EdFormatMissingKeyError(EdFormatPackageError):
    """An exported object lacks a property of the declared key type."""


class EdFormatShapeError(EdFormatPackageError):
    """A declaration is inconsistent or outside the supported XDTO profile."""
