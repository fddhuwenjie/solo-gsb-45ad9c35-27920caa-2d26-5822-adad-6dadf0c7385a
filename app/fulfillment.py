"""Fulfillment (evidence) checking: match submitted materials against the
obligation instances an ``ok`` assessment produced.

Pipeline for one fulfillment check:

1. normalize/validate the submitted materials and the evidence rules --
   sorting nothing semantically, but every derived ordering is based on
   sorted ids/contents so material/rule input order can never change the
   result;
2. expand each component's *in-scope* obligations (exactly the
   ``components[].obligations`` list of the assessment result) into one
   satisfaction instance per ``(component, obligation)`` pair;
3. evaluate, for every instance, whether the *set* of selected materials
   satisfies its evidence rule.  Rule leaves are accumulated over the
   whole selection: a leaf holds when **at least one** selected material
   matches it, so the conjuncts of an ``all`` (at any nesting depth) may
   be evidenced by different materials -- e.g. one signed attribution
   notice and one separate license-text copy jointly satisfy
   ``all[type=notice, type=license-copy]``.  A product-scoped material
   may cover any component, a component-scoped material only the
   component it declares; a rule-level ``component`` pin narrows a leaf
   to one component;
4. solve a *global* optimization -- never item-by-item greedy, never a
   fixed cut-off -- minimizing
   ``(unsatisfied instances, materials used, material-id tuple)``.
   Small spaces are enumerated exhaustively; larger spaces use a complete
   branch-and-bound search that only prunes provably non-improving
   subtrees, so both paths return the identical optimum.

The same material ``digest`` counts once; repeated digests whose metadata
differs are rejected up front (HTTP 422).
"""

from .common import ClientError, canonical, stable_hash


class UnprocessableError(ValueError):
    """The request is well-formed but semantically unusable (HTTP 422)."""


# ---------------------------------------------------------------------------
# validation / normalization
# ---------------------------------------------------------------------------

_SCOPES = ("product", "component")


def _material_signature(e):
    return stable_hash(
        {"type": e["type"], "scope": e["scope"], "component": e["component"]}
    )


def normalize_materials(body):
    """Validate and deduplicate the submitted materials.

    Returns ``(materials, duplicates)`` where *materials* maps the
    canonical id (smallest id sharing a digest) to::

        {"id", "digest", "type", "scope", "component" or None}

    Materials are returned in sorted-id order.  Equal digests with equal
    metadata collapse into one material; equal digests whose metadata
    conflicts raise :class:`UnprocessableError` (422).
    """
    raw_list = body.get("materials")
    if not isinstance(raw_list, list) or not raw_list:
        raise ClientError("materials must be a non-empty array")

    def require_str(obj, key, where, allow_empty=False):
        val = obj.get(key)
        if not isinstance(val, str) or (not allow_empty and not val.strip()):
            raise ClientError("%s.%s must be a non-empty string" % (where, key))
        return val.strip()

    entries = []
    seen_ids = set()
    for i, raw in enumerate(raw_list):
        where = "materials[%d]" % i
        if not isinstance(raw, dict):
            raise ClientError("%s must be an object" % where)
        mid = require_str(raw, "id", where)
        digest = require_str(raw, "digest", where)
        mtype = require_str(raw, "type", where)
        scope = require_str(raw, "scope", where)
        if scope not in _SCOPES:
            raise ClientError("%s.scope must be one of %s" % (where, list(_SCOPES)))
        component = None
        if scope == "component":
            component = require_str(raw, "component", where)
        elif raw.get("component") is not None:
            # a product-scoped material carries no component target; accepting
            # one would make the same digest mean different things
            raise ClientError(
                "%s: product-scoped materials must not declare a component" % where
            )
        if mid in seen_ids:
            raise ClientError("duplicate material id %r" % mid)
        seen_ids.add(mid)
        entries.append(
            {
                "id": mid,
                "digest": digest,
                "type": mtype,
                "scope": scope,
                "component": component,
            }
        )

    # group by digest -- the comparison is order-independent (dict grouping
    # then canonical serialization, no reliance on the input sequence)
    groups = {}
    order = []
    for e in entries:
        if e["digest"] not in groups:
            groups[e["digest"]] = []
            order.append(e["digest"])
        groups[e["digest"]].append(e)

    materials = {}
    duplicates = []
    for digest in order:
        members = groups[digest]
        sigs = {_material_signature(e) for e in members}
        if len(sigs) > 1:
            detail = sorted(
                "%s(type=%s, scope=%s, component=%s)"
                % (e["id"], e["type"], e["scope"], e["component"])
                for e in members
            )
            raise UnprocessableError(
                "digest %s submitted with conflicting metadata: %s"
                % (digest, "; ".join(detail))
            )
        canonical_id = min(e["id"] for e in members)
        keep = next(e for e in members if e["id"] == canonical_id)
        materials[canonical_id] = keep
        if len(members) > 1:
            duplicates.append(
                {
                    "digest": digest,
                    "canonical_id": canonical_id,
                    "duplicate_ids": sorted(e["id"] for e in members),
                }
            )

    duplicates.sort(key=lambda d: d["canonical_id"])
    return materials, duplicates


def normalize_rules(body, comp_ids):
    """Validate the evidence-rule payload; return a normalized rule tree.

    Shape::

        {"obligations": {"<obligation>": rule-node}}

    A rule node is either a nested ``{"all": [...]}`` / ``{"any": [...]}``
    node or a leaf ``{"type"?: str, "scope"?: "product"|"component",
    "component"?: str}``.  The returned tree is structurally identical to
    the input; its semantics depend only on node contents, never on array
    order.
    """
    raw = body.get("rules")
    if not isinstance(raw, dict):
        raise ClientError("rules must be an object")
    raw_obs = raw.get("obligations")
    if not isinstance(raw_obs, dict) or not raw_obs:
        raise ClientError("rules.obligations must be a non-empty object")

    valid_components = set(comp_ids)

    def norm_node(node, path):
        if not isinstance(node, dict):
            raise ClientError("%s must be an object" % path)
        keys = set(node)
        combinators = keys & {"all", "any"}
        if len(combinators) > 1:
            raise ClientError("%s: 'all' and 'any' are mutually exclusive" % path)
        if combinators:
            comb = next(iter(combinators))
            children = node[comb]
            if not isinstance(children, list) or not children:
                raise ClientError("%s.%s must be a non-empty array" % (path, comb))
            extra = keys - {comb}
            if extra:
                raise ClientError(
                    "%s: %s nodes may not carry leaf fields %s"
                    % (path, comb, sorted(extra))
                )
            return {comb: [norm_node(ch, "%s.%s[%d]" % (path, comb, i))
                           for i, ch in enumerate(children)]}
        # leaf
        unknown = keys - {"type", "scope", "component"}
        if unknown:
            raise ClientError("%s: unknown fields %s" % (path, sorted(unknown)))
        leaf = {}
        if "type" in node:
            if not isinstance(node["type"], str) or not node["type"].strip():
                raise ClientError("%s.type must be a non-empty string" % path)
            leaf["type"] = node["type"].strip()
        if "scope" in node:
            if node["scope"] not in _SCOPES:
                raise ClientError(
                    "%s.scope must be one of %s" % (path, list(_SCOPES))
                )
            leaf["scope"] = node["scope"]
        if "component" in node:
            if not isinstance(node["component"], str) or not node["component"].strip():
                raise ClientError("%s.component must be a non-empty string" % path)
            cid = node["component"].strip()
            if cid not in valid_components:
                raise ClientError(
                    "%s.component references unknown component %r" % (path, cid)
                )
            leaf["component"] = cid
        if not leaf:
            raise ClientError("%s: a leaf rule must constrain at least one field" % path)
        return leaf

    rules = {}
    for ob in sorted(raw_obs):
        if not isinstance(ob, str) or not ob:
            raise ClientError("obligation rule keys must be non-empty strings")
        rules[ob] = norm_node(raw_obs[ob], "rules.obligations[%s]" % ob)
    return {"obligations": rules}


# ---------------------------------------------------------------------------
# instance expansion
# ---------------------------------------------------------------------------

def expand_instances(result):
    """Expand an ``ok`` assessment result into satisfaction instances.

    One instance per in-scope ``(component, obligation)`` pair -- exactly
    the obligations listed on the component report (the distribution gate
    has already been applied by the assessment solver).
    """
    instances = []
    for crep in result.get("components", []):
        cid = crep["id"]
        for ob in crep.get("obligations", []):
            instances.append({"component": cid, "obligation": ob})
    instances.sort(key=lambda inst: (inst["component"], inst["obligation"]))
    return instances


# ---------------------------------------------------------------------------
# rule compilation and set-based leaf accumulation
# ---------------------------------------------------------------------------
#
# A compiled tree is a tuple:
#   ("a", (subtree, ...))      -- conjunction
#   ("o", (subtree, ...))      -- disjunction
#   ("l", leaf_index)          -- leaf reference
#
# Leaves are deduplicated by their (type, scope, component) constraint:
# the same leaf appearing under several branches is one proposition.
# A leaf proposition is true for a material selection iff at least one
# selected material matches the leaf -- satisfaction accumulates over
# the whole selected set, never evaluated against one material at a time.


def _leaf_key(leaf):
    return (leaf.get("type"), leaf.get("scope"), leaf.get("component"))


def compile_rule(node):
    """Compile a normalized rule node into ``(tree, leaves)``.

    *leaves* maps leaf index to the canonical leaf dict (only the fields
    the leaf constrains).
    """
    leaves = {}

    def rec(n):
        if "all" in n:
            return ("a", tuple(rec(ch) for ch in n["all"]))
        if "any" in n:
            return ("o", tuple(rec(ch) for ch in n["any"]))
        key = _leaf_key(n)
        idx = leaves.get(key)
        if idx is None:
            idx = len(leaves)
            leaves[key] = idx
        return ("l", idx)

    tree = rec(node)
    ordered = [None] * len(leaves)
    for (mtype, scope, component), idx in leaves.items():
        leaf = {}
        if mtype is not None:
            leaf["type"] = mtype
        if scope is not None:
            leaf["scope"] = scope
        if component is not None:
            leaf["component"] = component
        ordered[idx] = leaf
    return tree, ordered


def _leaf_matches(leaf, material, instance):
    if "type" in leaf and leaf["type"] != material["type"]:
        return False
    if "scope" in leaf and leaf["scope"] != material["scope"]:
        return False
    # a product-scoped material covers every applicable component; a
    # component-scoped material covers only the component it declares
    if material["scope"] == "component" and material["component"] != instance["component"]:
        return False
    # an explicit component constraint on the rule narrows even product scope
    if "component" in leaf and leaf["component"] != instance["component"]:
        return False
    return True


def flatten_tree(tree, nleaves):
    """Flatten a compiled tuple tree into a postorder boolean circuit.

    Gates occupy indexes 0..nleaves-1 for the leaves (gate index == leaf
    index) followed by combinator gates in postorder.  Each combinator
    gate is ``("a", input_mask)`` (all inputs must be true) or
    ``("o", input_mask)`` (at least one input must be true), with
    *input_mask* referencing earlier gate indexes.  Returns
    ``(gates, root_index)``; leaf gates are present only as slots so the
    bit positions line up -- :func:`eval_circuit` skips them.
    """
    gates = [("l", i) for i in range(nleaves)]
    combinators = []

    def rec(t):
        kind = t[0]
        if kind == "l":
            return t[1]
        inputs = [rec(ch) for ch in t[1]]
        gi = nleaves + len(combinators)
        mask = 0
        for j in inputs:
            mask |= 1 << j  # OR, not sum: a leaf may occur more than once
        combinators.append((kind, mask))
        return gi

    root = rec(tree)
    gates += combinators
    return gates, root


def eval_circuit(gates, root, leaf_truth, nleaves=None):
    """Evaluate a flattened circuit against a leaf-truth bitmask.

    Returns the root truth value.  Leaf gates occupy indexes
    0..nleaves-1 and mirror the proposition bits; combinator gates run
    against the earlier gate indexes they reference.  *nleaves* may be
    pre-supplied to avoid rescanning the gate prefix.  Combinator gates
    are evaluated lazily from the root with short-circuiting, so gates
    on branches that cannot affect the outcome are never computed.
    """
    if nleaves is None:
        nleaves = 0
        for kind, _ in gates:
            if kind == "l":
                nleaves += 1
            else:
                break
    values = leaf_truth
    known = (1 << nleaves) - 1  # leaf gates are known propositions

    def gate_true(i):
        nonlocal values, known
        if (known >> i) & 1:
            return ((values >> i) & 1) == 1
        kind, input_mask = gates[i]
        if kind == "a":
            v = all(gate_true(j) for j in _set_bits(input_mask))
        else:
            v = any(gate_true(j) for j in _set_bits(input_mask))
        known |= 1 << i
        if v:
            values |= 1 << i
        return v

    return gate_true(root)


def _set_bits(mask):
    while mask:
        b = mask & -mask
        yield b.bit_length() - 1
        mask ^= b


class CircuitState:
    """Incremental truth state for one compiled circuit.

    Maintains per-leaf witness counts (how many selected materials match
    each leaf) and the truth bitmask of every gate.  ``apply``/``remove``
    update one material's leaf bits and propagate only the combinator
    gates reachable from leaves whose truth actually flips, in fixed
    postorder order -- one forward sweep suffices because every gate's
    inputs precede it.
    """

    __slots__ = ("gates", "nleaves", "root_bit", "root_index", "counts",
                 "values", "is_and", "input_masks", "gate_bits")

    def __init__(self, gates, root):
        self.gates = gates
        nleaves = 0
        for kind, _ in gates:
            if kind == "l":
                nleaves += 1
            else:
                break
        self.nleaves = nleaves
        self.root_index = root
        self.root_bit = 1 << root
        self.counts = [0] * nleaves
        self.values = 0
        self.is_and = [kind == "a" for kind, _ in gates[nleaves:]]
        self.input_masks = [im for _kind, im in gates[nleaves:]]
        self.gate_bits = [
            1 << gi for gi in range(nleaves, len(gates))
        ]

    def _change(self, leaf_mask, adding):
        if not leaf_mask:
            return
        changed = 0
        m = leaf_mask
        while m:
            b = m & -m
            li = b.bit_length() - 1
            m ^= b
            c = self.counts[li]
            if adding:
                self.counts[li] = c + 1
                if c == 0:
                    self.values |= b
                    changed |= b
            else:
                self.counts[li] = c - 1
                if c == 1:
                    self.values &= ~b
                    changed |= b
        if not changed:
            return
        # forward sweep over combinator gates in postorder; a gate is
        # revisited only when one of its inputs flipped
        values = self.values
        is_and = self.is_and
        input_masks = self.input_masks
        gate_bits = self.gate_bits
        for off in range(len(is_and)):
            input_mask = input_masks[off]
            if not (input_mask & changed):
                continue
            bit = gate_bits[off]
            cur = bool(values & bit)
            if is_and[off]:
                v = (input_mask & ~values) == 0
            else:
                v = bool(input_mask & values)
            if v and not cur:
                values |= bit
                changed |= bit
            elif cur and not v:
                values &= ~bit
                changed |= bit
        self.values = values

    def apply(self, leaf_mask):
        self._change(leaf_mask, True)

    def remove(self, leaf_mask):
        self._change(leaf_mask, False)

    def true_with(self, leaf_mask):
        """Root truth if *leaf_mask* were OR-added (current state untouched)."""
        return eval_circuit(
            self.gates, self.root_index, self.values | leaf_mask, self.nleaves
        )

    def root(self):
        return bool(self.values & self.root_bit)


def build_formula(materials, material_ids, rule_node, instance):
    """Compile one instance's rule into a solver formula.

    Returns ``None`` when no evidence rule was submitted for the
    obligation; otherwise
    ``(tree, leaf_masks, leaves, gates, root, nleaves)`` where
    ``leaf_masks[j]`` is a bitmask of the rule leaves matched by
    material *j*.  A material matches at least one leaf iff its mask is
    non-zero; a leaf proposition for a selection is the OR of the masks
    of the selected materials (set accumulation), and the compiled
    *gates*/*root* circuit decides the instance from that leaf truth.
    """
    if rule_node is None:
        return None
    tree, leaves = compile_rule(rule_node)
    nleaves = len(leaves)
    gates, root = flatten_tree(tree, nleaves)
    leaf_masks = []
    for j, mid in enumerate(material_ids):
        material = materials[mid]
        mask = 0
        for li, leaf in enumerate(leaves):
            if _leaf_matches(leaf, material, instance):
                mask |= 1 << li
        leaf_masks.append(mask)
    return tree, leaf_masks, leaves, gates, root, nleaves


def _witness(tree, leaf_mats, selected_set):
    """Return the sorted tuple of selected materials evidencing *tree*,
    or ``None`` when the tree is not satisfied by *selected_set*.

    For an ``all`` every child must be evidenced; for an ``any`` the
    contributors of every true child are unioned.  A material appears
    only when it matches a leaf that participates in a satisfied
    portion of the rule -- a leaf under a dead branch never credits it.
    """
    kind = tree[0]
    if kind == "l":
        mats = leaf_mats[tree[1]]
        hit = tuple(m for m in mats if m in selected_set)
        return hit or None
    if kind == "a":
        contributors = set()
        for ch in tree[1]:
            part = _witness(ch, leaf_mats, selected_set)
            if part is None:
                return None
            contributors.update(part)
        return tuple(sorted(contributors))
    contributors = set()
    for ch in tree[1]:
        part = _witness(ch, leaf_mats, selected_set)
        if part is not None:
            contributors.update(part)
    return tuple(sorted(contributors)) or None


def _missing_leaves(tree, leaf_mats):
    """For a tree that is false over the available materials, return the
    set of leaf indexes whose absence blocks satisfaction; return
    ``None`` when the subtree itself is already satisfied:

    * at a leaf: the leaf itself when no material matches it;
    * under an ``all`` (tree false): blocking leaves of every false
      child -- satisfied children block nothing, even if they contain
      unmatched leaves on dead branches;
    * under an ``any`` (tree false): every child is false, so union the
      blocking leaves of all children.
    """
    kind = tree[0]
    if kind == "l":
        return None if leaf_mats[tree[1]] else {tree[1]}
    if kind == "a":
        missing = set()
        for ch in tree[1]:
            part = _missing_leaves(ch, leaf_mats)
            if part is not None:
                missing |= part
        return missing if missing else None
    missing = set()
    for ch in tree[1]:
        part = _missing_leaves(ch, leaf_mats)
        if part is None:
            # at least one satisfied disjunct: the subtree blocks nothing
            return None
        missing |= part
    return missing


def _iter_leaf_indexes(tree):
    kind = tree[0]
    if kind == "l":
        yield tree[1]
        return
    for ch in tree[1]:
        yield from _iter_leaf_indexes(ch)


# ---------------------------------------------------------------------------
# global optimization: exact minimum cover with set-valued leaf predicates
# and a lexicographic tie-break
# ---------------------------------------------------------------------------

def _popcount(mask):
    return bin(mask).count("1")


def _drop_dominated(material_ids, formulas):
    """Remove a material only when another strictly dominates it.

    Material *a* dominates *b* when, for **every** instance, the set of
    rule leaves *a* matches is a superset of those *b* matches, the
    inclusion is strict for at least one instance, and ``id(a) <=
    id(b)``.  Replacing *b* by *a* can then never lose a satisfied leaf
    proposition (monotone formulas cannot become false), while the id
    condition guarantees the lexicographic tie-break cannot worsen.
    Formulas for instances without an evidence rule are ignored (nothing
    can satisfy them regardless).  The outcome is order-free.
    """
    n = len(material_ids)
    active = [f is not None for f in formulas]
    keep = []
    for j in range(n):
        dominated = False
        for i in range(n):
            if i == j or material_ids[i] > material_ids[j]:
                continue
            strict = False
            ok = True
            for k, formula in enumerate(formulas):
                if not active[k]:
                    continue
                lm_i = formula[1][i]
                lm_j = formula[1][j]
                if lm_j & ~lm_i:
                    ok = False
                    break
                if lm_j != lm_i:
                    strict = True
            if ok and strict:
                dominated = True
                break
        if not dominated:
            keep.append(j)
    return [material_ids[j] for j in keep], [[formulas[k][1][j] for k in range(len(formulas))
                                             if active[k]] for j in keep]


def _build_states(gates_roots):
    return [CircuitState(gates, root) for gates, root, _nleaves in gates_roots]


def _states_covered(states):
    mask = 0
    for k, st in enumerate(states):
        if st.root():
            mask |= 1 << k
    return mask


def solve_exhaustive(gates_roots, material_ids, formula_leaf_masks):
    """Enumerate every subset in binary-reflected Gray-code order.

    *gates_roots*[k] is the compiled circuit of active instance *k*;
    *formula_leaf_masks*[k][j] is the leaf bitmask material *j* matches
    for that instance.  Consecutive Gray-code subsets differ in exactly
    one material, so per-instance gate truth is updated incrementally
    (one apply/remove between subsets, each root read is O(1)).  This is
    the reference oracle: no pruning, every subset evaluated.  Returns
    ``(covered_instance_mask, selected_ids)``.
    """
    n = len(material_ids)
    states = _build_states(gates_roots)
    best_key = None
    best_cov = 0

    chosen_mask = 0
    chosen_count = 0
    total = 1 << n
    for g in range(total):
        if g:
            # Gray transition from (g-1) to g flips material index
            # bit_length(g & -g) - 1
            j = (g & -g).bit_length() - 1
            add = not ((chosen_mask >> j) & 1)
            for k, st in enumerate(states):
                if add:
                    st.apply(formula_leaf_masks[k][j])
                else:
                    st.remove(formula_leaf_masks[k][j])
            if add:
                chosen_mask |= 1 << j
                chosen_count += 1
            else:
                chosen_mask &= ~(1 << j)
                chosen_count -= 1

        covered = 0
        for k, st in enumerate(states):
            if st.root():
                covered |= 1 << k
        tup = tuple(material_ids[j] for j in _set_bits(chosen_mask))
        key = (-_popcount(covered), chosen_count, tup)
        if best_key is None or key < best_key:
            best_key = key
            best_cov = covered

    return best_cov, best_key[2]


def solve_branch_bound(gates_roots, material_ids, formula_leaf_masks):
    """Complete branch-and-bound twin of :func:`solve_exhaustive`.

    Explores subsets include-first over ids sorted ascending, so equally
    good selections are found in ascending id-tuple order.  Per-instance
    gate truth is maintained incrementally.  A subtree is pruned only
    when it is provably unable to improve the incumbent:

    * even taking every remaining material it cannot reach the
      incumbent's covered-instance count;
    * when the incumbent's count can at best be tied, the admissible
      per-material gain bound says too many extra materials are needed,
      or the count ties while the current id tuple already exceeds the
      incumbent's (the tuple only grows on this branch).
    """
    n = len(material_ids)
    ni = len(gates_roots)
    full_inst = (1 << ni) - 1
    states = _build_states(gates_roots)

    # suffix leaf-truth per active instance: what remains reachable
    suffix_truth = [[0] * (n + 1) for _ in range(ni)]
    for k in range(ni):
        for i in range(n - 1, -1, -1):
            suffix_truth[k][i] = suffix_truth[k][i + 1] | formula_leaf_masks[k][i]

    # instances each material could possibly help (matches >=1 leaf there)
    material_reach = [0] * n
    for j in range(n):
        for k in range(ni):
            if formula_leaf_masks[k][j]:
                material_reach[j] |= 1 << k

    def covered_of():
        return _states_covered(states)

    def reachable_of(i):
        mask = 0
        for k, st in enumerate(states):
            if st.true_with(suffix_truth[k][i]):
                mask |= 1 << k
        return mask

    # greedy incumbent: each round pick the material that actually newly
    # satisfies the most still-unsatisfied instances (smallest id on
    # ties).  The seed never changes the optimum, only pruning strength.
    chosen_idx = set()
    seed_count = 0
    while True:
        cur = covered_of()
        best_j, best_gain = None, 0
        for j in range(n):
            if j in chosen_idx:
                continue
            gain = 0
            for k, st in enumerate(states):
                if not ((cur >> k) & 1) and st.true_with(formula_leaf_masks[k][j]):
                    gain += 1
            if gain > best_gain or (
                gain == best_gain and gain
                and (best_j is None or material_ids[j] < material_ids[best_j])
            ):
                best_j, best_gain = j, gain
        if best_j is None:
            break
        for k, st in enumerate(states):
            st.apply(formula_leaf_masks[k][best_j])
        chosen_idx.add(best_j)
        seed_count += 1
    seed_sel = tuple(sorted(material_ids[j] for j in chosen_idx))
    best = [-_popcount(covered_of()), seed_count, seed_sel]
    # restore a clean slate; the seed result is only the incumbent
    for j in chosen_idx:
        for k, st in enumerate(states):
            st.remove(formula_leaf_masks[k][j])

    def dfs(i, cnt, sel):
        cov = covered_of()
        if i == n:
            cand = (-_popcount(cov), cnt, sel)
            if cand < tuple(best):
                best[:] = list(cand)
            return

        reach = reachable_of(i)
        reach_count = _popcount(reach)
        if reach_count < -best[0]:
            return  # cannot even tie incumbent coverage

        if reach_count == -best[0]:
            # Coverage can at best tie the incumbent: count must strictly
            # improve, or merely tie with a smaller id tuple.  One
            # remaining material newly satisfies at most as many
            # uncovered instances as it has a matching leaf for -- an
            # admissible count lower bound.
            need = -best[0] - _popcount(cov)
            per = 0
            if need:
                uncovered = full_inst & ~cov
                for j in range(i, n):
                    gain = _popcount(material_reach[j] & uncovered)
                    if gain > per:
                        per = gain
                if per == 0:
                    return  # coverage cannot be reached at all
            lb_extra = (need + per - 1) // per if need else 0
            min_total = cnt + lb_extra
            if min_total > best[1] or (min_total == best[1] and sel >= best[2]):
                return

        # include first -> the first optimum encountered is already the
        # lexicographically smallest id tuple (purely a speed concern)
        for k, st in enumerate(states):
            st.apply(formula_leaf_masks[k][i])
        dfs(i + 1, cnt + 1, sel + (material_ids[i],))
        for k, st in enumerate(states):
            st.remove(formula_leaf_masks[k][i])
        dfs(i + 1, cnt, sel)

    dfs(0, 0, ())
    # recompute the winner's coverage deterministically from its id tuple
    index = {mid: j for j, mid in enumerate(material_ids)}
    covered = 0
    for k, (gates, root, nleaves) in enumerate(gates_roots):
        truth = 0
        for mid in best[2]:
            truth |= formula_leaf_masks[k][index[mid]]
        if eval_circuit(gates, root, truth, nleaves):
            covered |= 1 << k
    return covered, best[2]


def optimize(formulas, material_ids_all):
    """Pick the globally optimal material selection.

    *formulas[k]* is the compiled formula for instance *k* (or ``None``
    when no evidence rule covers the obligation).  Returns
    ``(selected_ids_tuple, mode, covered_instance_mask)``.
    """
    n_instances = len(formulas)

    material_ids, reduced = _drop_dominated(list(material_ids_all), formulas)
    active_idx = [k for k, f in enumerate(formulas) if f is not None]
    gates_roots = [(formulas[k][3], formulas[k][4], formulas[k][5]) for k in active_idx]
    # reduced[j] lists, per active instance, material j's leaf mask
    leaf_masks = [
        [reduced[j][t] for j in range(len(material_ids))]
        for t in range(len(active_idx))
    ]
    # drop materials that match no leaf of any rule -- they participate
    # in no optimum
    paired = [
        (mid, [leaf_masks[t][j] for t in range(len(active_idx))])
        for j, mid in enumerate(material_ids)
        if any(leaf_masks[t][j] for t in range(len(active_idx)))
    ]
    paired.sort(key=lambda p: p[0])
    material_ids = [p[0] for p in paired]
    leaf_masks = [
        [paired[j][1][t] for j in range(len(paired))]
        for t in range(len(active_idx))
    ]

    if not active_idx or not material_ids:
        return (), "exhaustive", 0

    if len(material_ids) <= 16:
        mode = "exhaustive"
        covered_active, selected = solve_exhaustive(
            gates_roots, material_ids, leaf_masks
        )
    else:
        mode = "branch-and-bound"
        covered_active, selected = solve_branch_bound(
            gates_roots, material_ids, leaf_masks
        )
    covered = 0
    for t, k in enumerate(active_idx):
        if (covered_active >> t) & 1:
            covered |= 1 << k
    return selected, mode, covered


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def check_fulfillment(body, result):
    """Run a fulfillment check of *body* against assessment *result*.

    The caller guarantees ``result["status"] == "ok"``; the gate lives in
    the API layer so a non-release assessment never reaches this code.
    """
    comp_ids = sorted(c["id"] for c in result.get("components", []))
    materials, duplicates = normalize_materials(body)
    rules = normalize_rules(body, comp_ids)
    instances = expand_instances(result)

    material_ids = sorted(materials)
    rule_by_ob = rules["obligations"]

    formulas = []
    for inst in instances:
        formulas.append(
            build_formula(
                materials,
                material_ids,
                rule_by_ob.get(inst["obligation"]),
                inst,
            )
        )

    if not instances:
        selected, mode, covered = (), "exhaustive", 0
    else:
        selected, mode, covered = optimize(formulas, material_ids)

    selected_set = set(selected)
    full = (1 << len(instances)) - 1 if instances else 0
    ready = covered == full

    # per-instance witnesses against the selected set and against every
    # submitted material (the latter drives accurate gap reporting)
    def instance_leaf_mats(formula):
        """material ids, per rule leaf, that match the leaf (sorted)."""
        if formula is None:
            return []
        _tree, lm, _leaves, _gates, _root, _nleaves = formula
        leaf_mats = []
        for li in range(len(_leaves)):
            leaf_mats.append(tuple(
                material_ids[j]
                for j in range(len(material_ids))
                if (lm[j] >> li) & 1
            ))
        return leaf_mats

    hit_by_material = {mid: [] for mid in selected}
    satisfied = []
    gaps = []
    for k, inst in enumerate(instances):
        formula = formulas[k]
        is_hit = ((covered >> k) & 1) == 1
        if formula is not None:
            tree, _lm, leaves, _gates, _root, _nleaves = formula
            leaf_mats = instance_leaf_mats(formula)
        else:
            tree, leaves, leaf_mats = None, [], []
        if is_hit:
            witness = _witness(tree, leaf_mats, selected_set)
            coverers = list(witness)
            satisfied.append(
                {
                    "component": inst["component"],
                    "obligation": inst["obligation"],
                    "satisfied_by": coverers,
                }
            )
            for mid in coverers:
                hit_by_material[mid].append(
                    {"component": inst["component"],
                     "obligation": inst["obligation"]}
                )
        else:
            if formula is None:
                reason = "no-evidence-rule"
                detail = "no evidence rule submitted for obligation %r" % inst["obligation"]
                candidate = []
                missing_leaf_dicts = []
            else:
                any_leaf_matched = any(
                    leaf_mats[li] for li in _iter_leaf_indexes(tree)
                )
                missing_idx = _missing_leaves(tree, leaf_mats)
                candidate = sorted({
                    mid
                    for li in _iter_leaf_indexes(tree)
                    for mid in leaf_mats[li]
                })
                missing_leaf_dicts = sorted(
                    (leaves[li] for li in missing_idx),
                    key=canonical,
                )
                if not any_leaf_matched:
                    reason = "no-matching-material"
                    detail = (
                        "no submitted material matches the evidence rule for "
                        "obligation %r at component %r"
                        % (inst["obligation"], inst["component"])
                    )
                    missing_leaf_dicts = []
                else:
                    reason = "incomplete-evidence"
                    detail = (
                        "the submitted materials only partially satisfy the "
                        "evidence rule for obligation %r at component %r; "
                        "%d leaf constraint(s) remain unmatched"
                        % (inst["obligation"], inst["component"],
                           len(missing_leaf_dicts))
                    )
            gaps.append(
                {
                    "component": inst["component"],
                    "obligation": inst["obligation"],
                    "reason": reason,
                    "candidate_materials": candidate,
                    "missing_leaves": missing_leaf_dicts,
                    "detail": detail,
                }
            )

    # hit trace: only selected materials, in selected order; each entry
    # lists the instances its evidence contributed to (witness members)
    hit_materials = [
        {
            "material_id": mid,
            "instances": sorted(
                hit_by_material[mid],
                key=lambda h: (h["component"], h["obligation"]),
            ),
        }
        for mid in selected
    ]

    unused = [mid for mid in material_ids if mid not in selected_set]

    out = {
        "ready": ready,
        "selection_mode": mode,
        "instances_total": len(instances),
        "instances_satisfied": _popcount(covered),
        "selected_materials": list(selected),
        "satisfied": satisfied,
        "gaps": gaps,
        "hits": hit_materials,
        "unused_materials": unused,
        "deduplicated": duplicates,
    }
    out["fulfillment_hash"] = stable_hash(out)
    return out
