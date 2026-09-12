"""
activation.py — Tala Cognitive Engine Room: Graph Activation Engine
====================================================================
Implements the Neural Node activation scoring formula and propagation
algorithm used by the Tala Cognitive Engine Room.

Activation Formula
------------------
::

    activation = (
        semantic_match
        × relevance_weight
        × confidence_weight
        × freshness_weight
        × provider_trust_multiplier
        × governance_multiplier
    )

Each term is a float in ``[0.0, 1.0]``; all inputs are clamped before
use so that callers never accidentally produce out-of-range scores.

Authority Boundary Law
----------------------
``execution_authority`` is set once at parse-time and is **immutable**.
The activation engine enforces this law via
:meth:`ActivationEngine.enforce_authority_boundary`, which raises
:class:`~tala.agent.exceptions.AuthorityBoundaryViolation` if anything
attempts to change a node's authority through activation logic.

Propagation
-----------
::

    propagated_activation = source_activation × relationship_weight × decay_factor

``decay_factor`` defaults to ``0.85`` — representing the information
loss incurred when spreading activation across one relationship hop.
The default can be overridden at :class:`ActivationEngine` construction
time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .exceptions import AuthorityBoundaryViolation

if TYPE_CHECKING:
    from .parser import NeuralNode


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DECAY_FACTOR: float = 0.85
"""
Default per-hop activation decay applied during propagation.

A value of 0.85 means that each relationship hop retains 85 % of the
source activation, modelling the reduced confidence of indirectly
inferred knowledge.
"""

_ACTIVATION_EPSILON: float = 1e-9
"""
Minimum activation value considered non-zero.  Activations below this
threshold are treated as ``0.0`` during propagation to prevent
floating-point underflow from accumulating noise.
"""


# ---------------------------------------------------------------------------
# Score inputs dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActivationInputs:
    """
    Named container for all inputs to the activation formula.

    All fields are clamped to ``[0.0, 1.0]`` by :class:`ActivationEngine`
    before the formula is evaluated.

    Attributes
    ----------
    semantic_match : float
        Cosine similarity (or equivalent) between the query embedding
        and this node's semantic memory embedding.
        *1.0* = perfect match, *0.0* = no overlap.
    relevance_weight : float
        The node's ``weight_relevance`` field — its intrinsic importance
        within the graph as declared at authoring time.
    confidence_weight : float
        Epistemic confidence in the node's content.  Sourced from
        provenance metadata or a confidence-scoring sub-system.
        *1.0* = fully verified, *0.0* = speculative.
    freshness_weight : float
        Temporal freshness of the node's content relative to query time.
        Computed externally (e.g. exponential decay over days since last
        update).  *1.0* = just updated, *0.0* = maximally stale.
    provider_trust_multiplier : float
        Trust score assigned to the information provider that authored or
        last verified this node.  Sourced from the provider registry.
        *1.0* = fully trusted, *0.0* = untrusted / blocked.
    governance_multiplier : float
        Governance gate value.  Typically derived from the node's
        ``execution_authority`` and any active policy constraints.
        *1.0* = fully authorised, *0.0* = governance-blocked.
    """

    semantic_match: float
    relevance_weight: float
    confidence_weight: float
    freshness_weight: float
    provider_trust_multiplier: float
    governance_multiplier: float


# ---------------------------------------------------------------------------
# Activation Engine
# ---------------------------------------------------------------------------


class ActivationEngine:
    """
    Stateless engine that computes Neural Node activation scores.

    Parameters
    ----------
    decay_factor : float
        Per-hop decay applied during :meth:`propagate`.  Must be in
        ``(0.0, 1.0]``.  Defaults to :data:`_DEFAULT_DECAY_FACTOR`.

    Examples
    --------
    >>> engine = ActivationEngine()
    >>> inputs = ActivationInputs(
    ...     semantic_match=0.9,
    ...     relevance_weight=0.8,
    ...     confidence_weight=0.95,
    ...     freshness_weight=0.7,
    ...     provider_trust_multiplier=1.0,
    ...     governance_multiplier=1.0,
    ... )
    >>> engine.score(inputs)
    0.47...
    """

    def __init__(self, decay_factor: float = _DEFAULT_DECAY_FACTOR) -> None:
        if not (0.0 < decay_factor <= 1.0):
            raise ValueError(
                f"decay_factor must be in (0.0, 1.0], got {decay_factor!r}."
            )
        self._decay_factor: float = decay_factor

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp(value: float) -> float:
        """Clamp *value* to ``[0.0, 1.0]``."""
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _clamp_inputs(inputs: ActivationInputs) -> ActivationInputs:
        """
        Return a new :class:`ActivationInputs` with all fields clamped to
        ``[0.0, 1.0]``.  This is called automatically by :meth:`score`
        so callers never need to pre-clamp their inputs.
        """
        clamp = ActivationEngine._clamp
        return ActivationInputs(
            semantic_match=clamp(inputs.semantic_match),
            relevance_weight=clamp(inputs.relevance_weight),
            confidence_weight=clamp(inputs.confidence_weight),
            freshness_weight=clamp(inputs.freshness_weight),
            provider_trust_multiplier=clamp(inputs.provider_trust_multiplier),
            governance_multiplier=clamp(inputs.governance_multiplier),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, inputs: ActivationInputs) -> float:
        """
        Compute the full activation score for a Neural Node.

        Formula
        -------
        ::

            activation = (
                semantic_match
                × relevance_weight
                × confidence_weight
                × freshness_weight
                × provider_trust_multiplier
                × governance_multiplier
            )

        All input terms are clamped to ``[0.0, 1.0]`` before evaluation.
        The result is therefore guaranteed to be in ``[0.0, 1.0]``.

        Parameters
        ----------
        inputs : ActivationInputs
            All six formula terms.  Out-of-range values are silently
            clamped (never raised as errors) to allow graceful degradation
            from upstream systems that may produce noisy scores.

        Returns
        -------
        float
            Activation score in ``[0.0, 1.0]``.

        Notes
        -----
        *semantic_match* is the dominant term — a zero semantic match
        drives the entire product to zero regardless of other weights.
        This is intentional: a node that is semantically irrelevant to the
        current query should never activate, no matter how authoritative.

        *governance_multiplier* acts as a hard gate: setting it to ``0.0``
        blocks activation entirely for governance-constrained nodes.
        """
        clamped: ActivationInputs = self._clamp_inputs(inputs)

        activation: float = (
            clamped.semantic_match
            * clamped.relevance_weight
            * clamped.confidence_weight
            * clamped.freshness_weight
            * clamped.provider_trust_multiplier
            * clamped.governance_multiplier
        )

        # Belt-and-suspenders clamp; mathematically should be in [0,1]
        # already given all inputs clamped, but floating-point can drift.
        return max(0.0, min(1.0, activation))

    def propagate(
        self,
        source_activation: float,
        relationship_weight: float,
        *,
        decay_factor: float | None = None,
    ) -> float:
        """
        Compute the activation to propagate to a related node.

        Formula
        -------
        ::

            propagated = source_activation × relationship_weight × decay_factor

        Parameters
        ----------
        source_activation : float
            Activation score of the source node (clamped to ``[0.0, 1.0]``).
        relationship_weight : float
            Semantic strength of the relationship edge in ``[0.0, 1.0]``.
            Typically derived from the ``data-relationship-weight`` attribute
            on the :class:`~tala.agent.parser.RelationshipAnchor`.
        decay_factor : float | None
            Override the instance-level decay factor for this call.
            Must be in ``(0.0, 1.0]``.  Defaults to the engine's
            ``decay_factor`` constructor argument.

        Returns
        -------
        float
            Propagated activation in ``[0.0, 1.0]``.

        Notes
        -----
        Propagated activations below :data:`_ACTIVATION_EPSILON` are
        returned as ``0.0`` to suppress floating-point underflow noise.
        """
        effective_decay: float = self._decay_factor
        if decay_factor is not None:
            if not (0.0 < decay_factor <= 1.0):
                raise ValueError(
                    f"decay_factor override must be in (0.0, 1.0], got {decay_factor!r}."
                )
            effective_decay = decay_factor

        src: float = self._clamp(source_activation)
        rel: float = self._clamp(relationship_weight)

        propagated: float = src * rel * effective_decay

        if propagated < _ACTIVATION_EPSILON:
            return 0.0

        return max(0.0, min(1.0, propagated))

    # ------------------------------------------------------------------
    # Authority Boundary Law enforcement
    # ------------------------------------------------------------------

    @staticmethod
    def enforce_authority_boundary(
        node: "NeuralNode",
        new_activation: float,
    ) -> None:
        """
        Verify that applying *new_activation* to *node* does not
        constitute an attempt to modify the node's ``execution_authority``.

        **Authority Boundary Law**: ``execution_authority`` is set once at
        parse-time and is permanently immutable.  No activation score,
        propagation cascade, or external signal may alter it.

        This method raises :class:`~tala.agent.exceptions.AuthorityBoundaryViolation`
        if *new_activation* would be used to overwrite the node's
        ``execution_authority``.  Callers must pass their intended write
        value as *new_activation* so this guard can evaluate it.

        In practice, valid activation consumers should **read**
        ``execution_authority`` to derive the ``governance_multiplier``
        input, but should **never write** back to it.

        Parameters
        ----------
        node : NeuralNode
            The node being evaluated.
        new_activation : float
            The activation value the caller intends to write.  If this is
            not equal to the node's current ``execution_authority``, a
            violation is raised — because any write attempt, even one that
            happens to produce the same value, violates the law.

        Raises
        ------
        AuthorityBoundaryViolation
            Always raised when this method is called with any
            *new_activation*, because the act of calling this method
            with intent to write indicates a policy violation.

        Notes
        -----
        This design is intentionally strict: the guard raises on *any*
        write attempt so that governance auditing is unambiguous.  There
        is no "it happens to be the same value" exception.
        """
        raise AuthorityBoundaryViolation(
            f"Activation engine attempted to modify execution_authority of "
            f"node {node.node_id!r}. The Authority Boundary Law prohibits "
            f"this: execution_authority is immutable once set at parse-time.",
            node_id=node.node_id,
            attempted_value=float(new_activation),
            current_authority=node.execution_authority,
        )

    def score_from_node(
        self,
        node: "NeuralNode",
        semantic_match: float,
        *,
        confidence_weight: float = 1.0,
        freshness_weight: float = 1.0,
        provider_trust_multiplier: float = 1.0,
    ) -> float:
        """
        Convenience method: score a node using its own weight and authority.

        This is the most common call pattern — the caller supplies only
        the query-time signals (``semantic_match``, ``confidence_weight``,
        ``freshness_weight``, ``provider_trust_multiplier``) and the engine
        reads ``weight_relevance`` and ``execution_authority`` from the node
        itself.

        ``execution_authority`` is used as the ``governance_multiplier``
        because a node with higher authority should contribute more strongly
        to the reasoning graph.

        Parameters
        ----------
        node : NeuralNode
            The node to score.
        semantic_match : float
            Query-time cosine similarity between query and node embeddings.
        confidence_weight : float
            Epistemic confidence in the node's content.
        freshness_weight : float
            Temporal freshness of the node's content.
        provider_trust_multiplier : float
            Trust score assigned to the node's content provider.

        Returns
        -------
        float
            Activation score in ``[0.0, 1.0]``.
        """
        inputs = ActivationInputs(
            semantic_match=semantic_match,
            relevance_weight=node.weight_relevance,
            confidence_weight=confidence_weight,
            freshness_weight=freshness_weight,
            provider_trust_multiplier=provider_trust_multiplier,
            governance_multiplier=max(node.execution_authority, node.memory_authority),
        )
        return self.score(inputs)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "ActivationEngine",
    "ActivationInputs",
]
