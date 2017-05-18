from logging import getLogger
from operator import attrgetter

from .action import EvaluationState
from .astar import AStarAlgorithm
from .utils import ListView

__all__ = "GoalBase", "ActionBase", "Planner", "ActionNode", "GoalNode", "GoalBase"

logger = getLogger(__name__)


class GoalBase:
    state = {}
    priority = 0

    def get_relevance(self, world_state):
        return self.priority

    def is_satisfied(self, world_state):
        for key, value in self.state.items():
            if world_state[key] != value:
                return False

        return True


class NodeBase:
    def __init__(self, current_state=None, goal_state=None):
        if current_state is None:
            current_state = {}

        if goal_state is None:
            goal_state = {}

        self.current_state = current_state
        self.goal_state = goal_state

    @property
    def unsatisfied_keys(self):
        """Return the keys of the unsatisfied state symbols between the goal and current state"""
        current_state = self.current_state
        return [k for k, v in self.goal_state.items() if not current_state[k] == v]

    def satisfies_goal_state(self, state):
        """Determine if provided state satisfies required goal state

        :param state: state to test
        """
        for key, goal_value in self.goal_state.items():
            try:
                current_value = state[key]

            except KeyError:
                return False

            if current_value != goal_value:
                return False

        return True


class GoalNode(NodeBase):
    """GOAP A* Goal Node"""

    def __repr__(self):
        return "<GOAPAStarGoalNode: {}>".format(self.goal_state)


class UnsatisfiableGoalEncountered(BaseException):
    pass


class ActionNode(NodeBase):
    """A* Node with associated GOAP action"""

    def __init__(self, action, current_state=None, goal_state=None):
        super().__init__(current_state, goal_state)

        self.action = action

    def __repr__(self):
        return "<GOAPAStarActionNode {}>".format(self.action)

    @classmethod
    def create_neighbour(cls, action, parent, world_state):
        current_state = parent.current_state.copy()
        goal_state = parent.goal_state.copy()

        cls._update_states_from_action(action, current_state, goal_state, world_state)

        return cls(action, current_state, goal_state)

    @staticmethod
    def _update_states_from_action(action, current_state, goal_state, world_state):
        """Update internal current and goal states, according to action effects and preconditions

        This step is used to backwards track from goal state, adding requirements of this action to goal, and
        applying effects to current state.
        A* search terminates once current state satisfies goal state

        Required for heuristic estimate.
        """
        # Get state of preconditions
        action_preconditions = action.preconditions

        # 1 Update current state from effects, resolve variables
        action.apply_effects(current_state, goal_state)

        # 2 Update goal state from action preconditions, resolve variables
        for key, value in action_preconditions.items():
            # If we are overwriting an existing goal, it must already be satisfied (goal[k]=current[k]),
            # or else this path is unsolveable!
            if key in goal_state:
                goal_value = goal_state[key]
                current_value = current_state[key]

                if current_value != goal_value:
                    raise UnsatisfiableGoalEncountered()

            # Preconditions can expose effect plugins to a dependency
            if hasattr(value, "forward_effect_name"):
                value = current_state[value.forward_effect_name]

            goal_state[key] = value

            if key not in current_state:
                current_state[key] = world_state[key]


class ActionPlanStep:
    """Container object for bound action and its goal state"""

    def __init__(self, action, state):
        self.action = action
        self.goal_state = state

    def __repr__(self):
        return repr(self.action)


class ActionPlan:
    """Manager of a series of Actions which fulfil a goal state"""

    def __init__(self, plan_steps):
        self._plan_steps = plan_steps
        self._plan_steps_it = iter(plan_steps)
        self.plan_steps = ListView(self._plan_steps)
        self.current_plan_step = None

    def __repr__(self):
        def repr_step(step):
            return ("{!r}*" if step is self.current_plan_step else "{!r}").format(step)

        return "GOAPAIPlan :: {{{}}}".format(" -> ".join([repr_step(s) for s in self._plan_steps]))

    def cancel(self, world_state):
        try:
            # Cancel running step
            current_step = self.current_plan_step
            if current_step:
                current_step.action.on_exit(world_state, current_step.goal_state)
                self.current_plan_step = None

        finally:
            # Consume steps iterator
            for _ in self._plan_steps_it:
                pass

    def update(self, world_state):
        """Update the plan, ensuring it is valid

        :param world_state: world_state object
        """
        finished_state = EvaluationState.success
        running_state = EvaluationState.running

        current_step = self.current_plan_step

        while True:
            # If existing step is being processed
            if current_step is not None:
                action = current_step.action
                goal_state = current_step.goal_state

                # Check its state
                action_state = action.get_status(world_state, goal_state)

                # If the plan isn't finished, return its state (failure / running)
                if action_state == running_state:
                    return running_state

                # Leave previous step
                action.on_exit(world_state, goal_state)

                # Apply effects, but some actions deliberately prevent this because they do not solve symbolic problems
                if action.apply_effects_on_exit:
                    action.apply_effects(world_state, goal_state)

                # Return if not finished (e.g if error occurred)
                if action_state != finished_state:
                    return action_state

            # Get next step
            try:
                current_step = self.current_plan_step = next(self._plan_steps_it)

            except StopIteration:
                return EvaluationState.success

            action = current_step.action
            goal_state = current_step.goal_state

            # Check preconditions
            if not action.check_procedural_precondition(world_state, goal_state, is_planning=False):
                return EvaluationState.failure

            # Enter step
            action.on_enter(world_state, goal_state)

        return finished_state


_key_action_precedence = attrgetter("action.precedence")


class Planner(AStarAlgorithm):
    def __init__(self, actions, world_state):
        self.actions = actions
        self.world_state = world_state

        self.effects_to_actions = self.create_effect_table(actions)

    def find_plan_for_goal(self, goal_state):
        """Find shortest plan to produce goal state

        :param goal_state: state of goal
        """
        world_state = self.world_state

        goal_node = GoalNode()
        goal_node.current_state = {k: world_state.get(k) for k in goal_state}
        goal_node.goal_state = goal_state

        node_path = self.find_path(goal_node)

        node_path_next = iter(node_path)
        next(node_path_next)

        plan_steps = [ActionPlanStep(node.action, next_node.goal_state) for next_node, node in
                      zip(node_path, node_path_next)]
        plan_steps.reverse()

        return ActionPlan(plan_steps)

    def get_h_score(self, node, goal):
        """Rough estimate of cost of node, based upon satisfaction of goal state

        :param node: node to evaluate heuristic
        """
        return len(node.unsatisfied_keys)

    def get_g_score(self, current, node):
        """Determine procedural (or static) cost of this action

        :param current: node to move from (unused)
        :param node: node to move to (unused)
        """
        # Update world states
        return node.action.get_cost(self.world_state, node.goal_state)

    def get_neighbours(self, node):
        """Return new nodes for given node which satisfy missing state

        :param node: node performing request
        """
        effects_to_actions = self.effects_to_actions
        world_state = self.world_state
        goal_state = node.goal_state
        node_action = getattr(node, "action", None)

        neighbours = []
        for effect in node.unsatisfied_keys:
            try:
                actions = effects_to_actions[effect]

            except KeyError:
                continue

            # Create new node instances for every node
            for action in actions:
                if action is node_action:
                    continue

                if not action.check_procedural_precondition(world_state, goal_state, is_planning=True):
                    continue

                # Try and create a neighbour node, except if the goal could not be satisfied
                try:
                    neighbour = ActionNode.create_neighbour(action, node, world_state)

                except UnsatisfiableGoalEncountered:
                    continue

                neighbours.append(neighbour)

        neighbours.sort(key=_key_action_precedence)
        return neighbours

    @staticmethod
    def create_effect_table(actions):
        """Associate effects with appropriate actions

        :param actions: valid action instances
        """
        effect_to_actions = {}

        for action in actions:
            for effect in action.effects:
                try:
                    effect_classes = effect_to_actions[effect]

                except KeyError:
                    effect_classes = effect_to_actions[effect] = []

                effect_classes.append(action)

        return effect_to_actions

    def is_finished(self, node, goal, node_to_parent):
        if node.unsatisfied_keys:
            return False

        # Avoid copying whole state dict, leverage property that keys(node) should include all keys involved in search
        current_state = {k: self.world_state[k] for k in node.current_state}

        while node is not goal:
            node.action.apply_effects(current_state, node.goal_state)
            node = node_to_parent[node]

        return goal.satisfies_goal_state(current_state)