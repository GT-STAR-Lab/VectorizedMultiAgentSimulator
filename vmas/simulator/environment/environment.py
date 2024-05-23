#  Copyright (c) 2022-2024.
#  ProrokLab (https://www.proroklab.org/)
#  All rights reserved.
import random
from ctypes import byref
from typing import List, Tuple, Callable, Optional, Union, Dict

import numpy as np
import torch
from gym import spaces
from torch import Tensor
from vmas.simulator.core import Agent, TorchVectorizedObject
from vmas.simulator.scenario import BaseScenario
import vmas.simulator.utils
from vmas.simulator.utils import (
    AGENT_OBS_TYPE,
    X,
    Y,
    ALPHABET,
    DEVICE_TYPING,
    override,
    TorchUtils,
)


# environment for all agents in the multiagent world
# currently code assumes that no agents will be created/destroyed at runtime!
class Environment(TorchVectorizedObject):
    metadata = {
        "render.modes": ["human", "rgb_array"],
        "runtime.vectorized": True,
    }

    def __init__(
        self,
        scenario: BaseScenario,
        num_envs: int = 32,
        device: DEVICE_TYPING = "cpu",
        max_steps: Optional[int] = None,
        continuous_actions: bool = True,
        seed: Optional[int] = None,
        dict_spaces: bool = False,
        multidiscrete_actions: bool = False,
        clamp_actions: bool = False,
        **kwargs,
    ):
        if multidiscrete_actions:
            assert (
                not continuous_actions
            ), "When asking for multidiscrete_actions, make sure continuous_actions=False"

        self.scenario = scenario
        self.num_envs = num_envs
        TorchVectorizedObject.__init__(self, num_envs, torch.device(device))
        self.world = self.scenario.env_make_world(self.num_envs, self.device, **kwargs)

        self.agents = self.world.policy_agents
        self.n_agents = len(self.agents)
        self.max_steps = max_steps
        self.continuous_actions = continuous_actions
        self.dict_spaces = dict_spaces
        self.clamp_action = clamp_actions

        self.reset(seed=seed)

        # configure spaces
        self.multidiscrete_actions = multidiscrete_actions
        self.action_space = self.get_action_space()
        self.observation_space = self.get_observation_space()
        self.curiosity_state_space = self.get_curiosity_state_space() if hasattr(self.scenario, 'curiosity_state') else None
        self.environment_state_space = self.get_environment_state_space() if hasattr(self.scenario, 'environment_state') else None
        # print(self.curiosity_state_space)
        # rendering
        self.viewer = None
        self.headless = None
        self.visible_display = None
        self.text_lines = None

    def reset(
        self,
        seed: Optional[int] = None,
        return_observations: bool = True,
        return_info: bool = False,
        return_dones: bool = False,
    ):
        """
        Resets the environment in a vectorized way
        Returns observations for all envs and agents
        """
        if seed is not None:
            self.seed(seed)
        # reset world
        self.scenario.env_reset_world_at(env_index=None)
        self.steps = torch.zeros(self.num_envs, device=self.device)

        result = self.get_from_scenario(
            get_observations=return_observations,
            get_infos=return_info,
            get_rewards=False,
            get_dones=return_dones,
        )
        return result[0] if result and len(result) == 1 else result

    def reset_at(
        self,
        index: int,
        return_observations: bool = True,
        return_info: bool = False,
        return_dones: bool = False,
    ):
        """
        Resets the environment at index
        Returns observations for all agents in that environment
        """
        self._check_batch_index(index)
        self.scenario.env_reset_world_at(index)
        self.steps[index] = 0

        result = self.get_from_scenario(
            get_observations=return_observations,
            get_infos=return_info,
            get_rewards=False,
            get_dones=return_dones,
        )

        return result[0] if result and len(result) == 1 else result

    def get_from_scenario(
        self,
        get_observations: bool,
        get_rewards: bool,
        get_infos: bool,
        get_dones: bool,
        dict_agent_names: Optional[bool] = None,
    ):
        if not get_infos and not get_dones and not get_rewards and not get_observations:
            return
        if dict_agent_names is None:
            dict_agent_names = self.dict_spaces

        obs = rewards = infos = dones = None

        if get_observations:
            obs = {} if dict_agent_names else []
        if get_rewards:
            rewards = {} if dict_agent_names else []
        if get_infos:
            infos = {} if dict_agent_names else []

        for agent in self.agents:
            if get_rewards:
                reward = self.scenario.reward(agent).clone()
                if dict_agent_names:
                    rewards.update({agent.name: reward})
                else:
                    rewards.append(reward)
            if get_observations:
                observation = TorchUtils.recursive_clone(
                    self.scenario.observation(agent)
                )
                if dict_agent_names:
                    obs.update({agent.name: observation})
                else:
                    obs.append(observation)
            if get_infos:
                info = TorchUtils.recursive_clone(self.scenario.info(agent))
                if dict_agent_names:
                    infos.update({agent.name: info})
                else:
                    infos.append(info)

        if get_dones:
            dones = self.done()

        result = [obs, rewards, dones, infos]
        return [data for data in result if data is not None]

    def seed(self, seed=None):
        if seed is None:
            seed = 0
        torch.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)
        return [seed]

    def step(self, actions: Union[List, Dict]):
        """Performs a vectorized step on all sub environments using `actions`.
        Args:
            actions: Is a list on len 'self.n_agents' of which each element is a torch.Tensor of shape
            '(self.num_envs, action_size_of_agent)'.
        Returns:
            obs: List on len 'self.n_agents' of which each element is a torch.Tensor
                 of shape '(self.num_envs, obs_size_of_agent)'
            rewards: List on len 'self.n_agents' of which each element is a torch.Tensor of shape '(self.num_envs)'
            dones: Tensor of len 'self.num_envs' of which each element is a bool
            infos : List on len 'self.n_agents' of which each element is a dictionary for which each key is a metric
                    and the value is a tensor of shape '(self.num_envs, metric_size_per_agent)'
        """
        if isinstance(actions, Dict):
            actions_dict = actions
            actions = []
            for agent in self.agents:
                try:
                    actions.append(actions_dict[agent.name])
                except KeyError:
                    raise AssertionError(
                        f"Agent '{agent.name}' not contained in action dict"
                    )
            assert (
                len(actions_dict) == self.n_agents
            ), f"Expecting actions for {self.n_agents}, got {len(actions_dict)} actions"

        assert (
            len(actions) == self.n_agents
        ), f"Expecting actions for {self.n_agents}, got {len(actions)} actions"
        for i in range(len(actions)):
            if not isinstance(actions[i], Tensor):
                actions[i] = torch.tensor(
                    actions[i], dtype=torch.float32, device=self.device
                )
            if len(actions[i].shape) == 1:
                actions[i].unsqueeze_(-1)
            assert (
                actions[i].shape[0] == self.num_envs
            ), f"Actions used in input of env must be of len {self.num_envs}, got {actions[i].shape[0]}"
            assert actions[i].shape[1] == self.get_agent_action_size(self.agents[i]), (
                f"Action for agent {self.agents[i].name} has shape {actions[i].shape[1]},"
                f" but should have shape {self.get_agent_action_size(self.agents[i])}"
            )

        # set action for each agent
        for i, agent in enumerate(self.agents):
            self._set_action(actions[i], agent)
        # Scenarios can define a custom action processor. This step takes care also of scripted agents automatically
        for agent in self.world.agents:
            self.scenario.env_process_action(agent)

        # advance world state
        self.world.step()

        self.steps += 1
        obs, rewards, dones, infos = self.get_from_scenario(
            get_observations=True, get_infos=True, get_rewards=True, get_dones=True
        )

        # print("\nStep results in unwrapped environment")
        # print(
        #     f"Actions len (n_agents): {len(actions)}, "
        #     f"actions[0] shape (num_envs, agent 0 action shape): {actions[0].shape}, "
        #     f"actions[0][0] (action agent 0 env 0): {actions[0][0]}"
        # )
        # print(
        #     f"Obs len (n_agents): {len(obs)}, "
        #     f"obs[0] shape (num_envs, agent 0 obs shape): {obs[0].shape}, obs[0][0] (obs agent 0 env 0): {obs[0][0]}"
        # )
        # print(
        #     f"Rewards len (n_agents): {len(rewards)}, rewards[0] shape (num_envs, 1): {rewards[0].shape}, "
        #     f"rewards[0][0] (agent 0 env 0): {rewards[0][0]}"
        # )
        # print(f"Dones len (n_envs): {len(dones)}, dones[0] (done env 0): {dones[0]}")
        # print(f"Info len (n_agents): {len(infos)}, info[0] (infos agent 0): {infos[0]}")
        return obs, rewards, dones, infos

    def done(self):
        dones = self.scenario.done().clone()
        if self.max_steps is not None:
            dones += self.steps >= self.max_steps
        return dones

    def get_action_space(self):
        if not self.dict_spaces:
            return spaces.Tuple(
                [self.get_agent_action_space(agent) for agent in self.agents]
            )
        else:
            return spaces.Dict(
                {
                    agent.name: self.get_agent_action_space(agent)
                    for agent in self.agents
                }
            )

    def get_observation_space(self):
        if not self.dict_spaces:
            return spaces.Tuple(
                [
                    self.get_agent_observation_space(
                        agent, self.scenario.observation(agent)
                    )
                    for agent in self.agents
                ]
            )
        else:
            return spaces.Dict(
                {
                    agent.name: self.get_agent_observation_space(
                        agent, self.scenario.observation(agent)
                    )
                    for agent in self.agents
                }
            )
        
    def get_curiosity_state_space(self):
        if not self.dict_spaces:
            return spaces.Tuple(
                [
                    self.get_agent_curiosity_state_space(
                        agent, self.scenario.curiosity_state(agent)
                    )
                    for agent in self.agents
                ]
            )
        else:
            return spaces.Dict(
                {
                    agent.name: self.get_agent_curiosity_state_space(
                        agent, self.scenario.curiosity_state(agent)
                    )
                    for agent in self.agents
                }
            )
    def get_environment_state_space(self):
        agent = self.agents[0]
        if not self.dict_spaces:
            return spaces.Tuple(
                [
                    self.get_agent_environment_state_space(
                        agent, *self.scenario.environment_state(agent)
                    )
                ]
            )
        else:
            return spaces.Dict(
                {
                    agent.name: self.get_agent_environment_state_space(
                        agent, *self.scenario.environment_state(agent)
                    )
                }
            )
        
    def get_agent_action_size(self, agent: Agent):
        if self.continuous_actions:
            return agent.action.action_size + (
                self.world.dim_c if not agent.silent else 0
            )
        elif self.multidiscrete_actions:
            return agent.action_size + (
                1 if not agent.silent and self.world.dim_c != 0 else 0
            )
        else:
            return 1

    def get_agent_action_space(self, agent: Agent):
        if self.continuous_actions:
            return spaces.Box(
                low=np.array(
                    (-agent.action.u_range_tensor).tolist()
                    + [0] * (self.world.dim_c if not agent.silent else 0),
                    dtype=np.float32,
                ),
                high=np.array(
                    agent.action.u_range_tensor.tolist()
                    + [1] * (self.world.dim_c if not agent.silent else 0),
                    dtype=np.float32,
                ),
                shape=(self.get_agent_action_size(agent),),
                dtype=np.float32,
            )
        elif self.multidiscrete_actions:
            actions = [3] * agent.action_size + (
                [self.world.dim_c] if not agent.silent and self.world.dim_c != 0 else []
            )
            return spaces.MultiDiscrete(actions)
        else:
            return spaces.Discrete(
                3**agent.action_size
                * (
                    self.world.dim_c
                    if not agent.silent and self.world.dim_c != 0
                    else 1
                )
            )

    def get_agent_observation_space(self, agent: Agent, obs: AGENT_OBS_TYPE):
        if isinstance(obs, Tensor):
            return spaces.Box(
                low=-np.float32("inf"),
                high=np.float32("inf"),
                shape=(len(obs[0]),),
                dtype=np.float32,
            )
        elif isinstance(obs, Dict):
            return spaces.Dict(
                {
                    key: self.get_agent_observation_space(agent, value)
                    for key, value in obs.items()
                }
            )
        else:
            raise NotImplementedError(
                f"Invalid type of observation {obs} for agent {agent.name}"
            )
        
    def get_agent_curiosity_state_space(self, agent: Agent, obs: AGENT_OBS_TYPE):
        if isinstance(obs, Tensor):
            return spaces.Box(
                low=-np.float32("inf"),
                high=np.float32("inf"),
                shape=(len(obs[0]),),
                dtype=np.float32,
            )
        elif isinstance(obs, Dict):
            return spaces.Dict(
                {
                    key: self.get_agent_curiosity_state_space(agent, value)
                    for key, value in obs.items()
                }
            )
        else:
            raise NotImplementedError(
                f"Invalid type of curiosity_state {obs} for agent {agent.name}"
            )
        
    def get_agent_environment_state_space(self, agent: Agent, obs_1: AGENT_OBS_TYPE, obs_2: AGENT_OBS_TYPE):
        if isinstance(obs_1, Tensor):
            return spaces.Box(
                low=-np.float32("inf"),
                high=np.float32("inf"),
                shape=(len(obs_1[0]),len(obs_2[0])),
                dtype=np.float32,
            )
        elif isinstance(obs, Dict):
            return spaces.Dict(
                {
                    key: self.get_agent_environment_state_space(agent, value)
                    for key, value in obs.items()
                }
            )
        else:
            raise NotImplementedError(
                f"Invalid type of environment_state {obs} for agent {agent.name}"
            )

    def _check_discrete_action(self, action: Tensor, low: int, high: int, type: str):
        assert torch.all(
            (action >= torch.tensor(low, device=self.device))
            * (action < torch.tensor(high, device=self.device))
        ), f"Discrete {type} actions are out of bounds, allowed int range [{low},{high})"

    # set env action for a particular agent
    def _set_action(self, action, agent):
        action = action.clone().detach().to(self.device)
        assert not action.isnan().any()
        agent.action.u = torch.zeros(
            self.batch_dim, agent.action_size, device=self.device, dtype=torch.float32
        )

        assert action.shape[1] == self.get_agent_action_size(agent), (
            f"Agent {agent.name} has wrong action size, got {action.shape[1]}, "
            f"expected {self.get_agent_action_size(agent)}"
        )
        if self.clamp_action and self.continuous_actions:
            action = action.clamp(-agent.action.u_range, agent.action.u_range)

        action_index = 0

        if self.continuous_actions:
            physical_action = action[:, action_index : action_index + agent.action_size]
            action_index += self.world.dim_p
            assert not torch.any(
                torch.abs(physical_action) > agent.action.u_range_tensor
            ), f"Physical actions of agent {agent.name} are out of its range {agent.u_range}"

            agent.action.u = physical_action.to(torch.float32)

        else:
            if not self.multidiscrete_actions:
                # This bit of code translates the discrete action (taken from a space that
                # is the cartesian product of all action spaces) into a multi discrete action.
                # For example, if agent.action_size=4, it will mean that the agent will have
                # 4 actions each with 3 possibilities (stay, decrement, increment).
                # The env will have a space Discrete(3**4).
                # This code will translate the action (with shape [n_envs,1] and range [0,3**4)) to an
                # action with shape [n_envs,4] and range [0,3).
                n_actions = self.get_agent_action_space(agent).n
                action_range = torch.arange(n_actions, device=self.device).expand(
                    self.world.batch_dim, n_actions
                )
                physical_action = action
                action_range = torch.where(action_range == physical_action, 1.0, 0.0)
                action_range = action_range.view(
                    (self.world.batch_dim,)
                    + (3,) * agent.action_size
                    + (self.world.dim_c,)
                    * (1 if not agent.silent and self.world.dim_c != 0 else 0)
                )
                action = action_range.nonzero()[:, 1:]

            # Now we have an action with shape [n_envs, action_size+comms_actions]
            for _ in range(agent.action_size):
                physical_action = action[:, action_index].unsqueeze(-1)
                self._check_discrete_action(
                    physical_action,
                    low=0,
                    high=3,
                    type="physical",
                )

                arr1 = physical_action == 1
                arr2 = physical_action == 2

                disc_action_value = agent.action.u_range_tensor[action_index]
                agent.action.u[:, action_index] -= disc_action_value * arr1.squeeze(-1)
                agent.action.u[:, action_index] += disc_action_value * arr2.squeeze(-1)

                action_index += 1

        agent.action.u *= agent.action.u_multiplier_tensor

        if agent.action.u_noise > 0:
            noise = (
                torch.randn(
                    *agent.action.u.shape, device=self.device, dtype=torch.float32
                )
                * agent.u_noise
            )
            agent.action.u += noise
        if self.world.dim_c > 0 and not agent.silent:
            if not self.continuous_actions:
                comm_action = action[:, action_index:]
                self._check_discrete_action(
                    comm_action, 0, self.world.dim_c, "communication"
                )
                comm_action = comm_action.long()
                agent.action.c = torch.zeros(
                    self.num_envs,
                    self.world.dim_c,
                    device=self.device,
                    dtype=torch.float32,
                )
                # Discrete to one-hot
                agent.action.c.scatter_(1, comm_action, 1)
            else:
                comm_action = action[:, action_index:]
                assert not torch.any(comm_action > 1) and not torch.any(
                    comm_action < 0
                ), "Comm actions are out of range [0,1]"
                agent.action.c = comm_action
            if agent.c_noise > 0:
                noise = (
                    torch.randn(
                        *agent.action.c.shape, device=self.device, dtype=torch.float32
                    )
                    * agent.c_noise
                )
                agent.action.c += noise

    def render(
        self,
        mode="human",
        env_index=0,
        agent_index_focus: int = None,
        scene_focus: bool = True,
        visualize_when_rgb: bool = False,
        plot_position_function: Callable = None,
        plot_position_function_precision: float = 0.01,
        plot_position_function_range: Optional[
            Union[
                float,
                Tuple[float, float],
                Tuple[Tuple[float, float], Tuple[float, float]],
            ]
        ] = None,
        plot_position_function_cmap_range: Optional[Tuple[float, float]] = None,
        plot_position_function_cmap_alpha: Optional[float] = 1.0,
        plot_position_function_cmap_name: Optional[str] = "viridis",
    ):
        """
        Render function for environment using pyglet

        On servers use mode="rgb_array" and set
        ```
        export DISPLAY=':99.0'
        Xvfb :99 -screen 0 1400x900x24 > /dev/null 2>&1 &
        ```

        :param mode: One of human or rgb_array
        :param env_index: Index of the environment to render
        :param agent_index_focus: If specified the camera will stay on the agent with this index.
                                  If None, the camera will stay in the center and zoom out to contain all agents
        :param scene_focus: If specified, the camera will center on the centroid of the scene and zoom out to include all entities
        :param visualize_when_rgb: Also run human visualization when mode=="rgb_array"
        :param plot_position_function: A function to plot under the rendering.
        The function takes a numpy array with shape (n_points, 2), which represents a set of x,y values to evaluate f over and plot it
        It should output either an array with shape (n_points, 1) which will be plotted as a colormap
        or an array with shape (n_points, 4), which will be plotted as RGBA values
        :param plot_position_function_precision: The precision to use for plotting the function
        :param plot_position_function_range: The position range to plot the function in.
        If float, the range for x and y is (-function_range, function_range)
        If Tuple[float, float], the range for x is (-function_range[0], function_range[0]) and y is (-function_range[1], function_range[1])
        If Tuple[Tuple[float, float], Tuple[float, float]], the first tuple is the x range and the second tuple is the y range
        :param plot_position_function_cmap_range: The range of the cmap in case plot_position_function outputs a single value
        :param plot_position_function_cmap_alpha: The alpha of the cmap in case plot_position_function outputs a single value
        :return: Rgb array or None, depending on the mode
        """
        self._check_batch_index(env_index)
        assert (
            mode in self.metadata["render.modes"]
        ), f"Invalid mode {mode} received, allowed modes: {self.metadata['render.modes']}"
        if agent_index_focus is not None:
            assert 0 <= agent_index_focus < self.n_agents, (
                f"Agent focus in rendering should be a valid agent index"
                f" between 0 and {self.n_agents}, got {agent_index_focus}"
            )
        shared_viewer = agent_index_focus is None
        aspect_ratio = self.scenario.viewer_size[X] / self.scenario.viewer_size[Y]

        headless = mode == "rgb_array" and not visualize_when_rgb
        # First time rendering
        if self.visible_display is None:
            self.visible_display = not headless
            self.headless = headless
        # All other times headless should be the same
        else:
            assert self.visible_display is not headless

        # First time rendering
        if self.viewer is None:
            try:
                import pyglet
            except ImportError:
                raise ImportError(
                    "Cannot import pyg;et: you can install pyglet directly via 'pip install pyglet'."
                )

            try:
                # Try to use EGL
                pyglet.lib.load_library("EGL")

                # Only if we have GPUs
                from pyglet.libs.egl import egl
                from pyglet.libs.egl import eglext

                num_devices = egl.EGLint()
                eglext.eglQueryDevicesEXT(0, None, byref(num_devices))
                assert num_devices.value > 0

            except (ImportError, AssertionError):
                self.headless = False
            pyglet.options["headless"] = self.headless

            self._init_rendering()

        if self.scenario.viewer_zoom <= 0:
            raise ValueError("Scenario viewer zoom must be > 0")
        zoom = self.scenario.viewer_zoom

        if aspect_ratio < 1:
            cam_range = torch.tensor([zoom, zoom / aspect_ratio], device=self.device)
        else:
            cam_range = torch.tensor([zoom * aspect_ratio, zoom], device=self.device)

        if scene_focus:
            # get all poses in scene
            all_poses = torch.stack(
                [entity.state.pos[env_index] for entity in self.world.landmarks], 
                dim=0
            )

            # get max radius of entities in scene
            max_shape_radius = max(
                [entity.shape.circumscribed_radius() for entity in self.world.landmarks]
            )
            max_shape_radius = max(0.5, max_shape_radius)

            # get centroid of scene
            centroid = torch.mean(all_poses, dim=0)

            # get max distance from centroid
            side = torch.max(torch.tensor([
                    torch.max(torch.max(all_poses[:,X]) - centroid[X]),
                    torch.max(torch.max(all_poses[:,Y]) - centroid[Y]),
                    torch.max(centroid[X] - torch.min(all_poses[:, X])),
                    torch.max(centroid[Y] - torch.min(all_poses[:, Y]))
                ])
            )

            # set viewer to be square centered around origin + encopassing full world semidim
            self.viewer.set_bounds(
                torch.tensor(-(zoom * self.scenario.world_semidim) + self.scenario.render_origin[X], device=self.device),
                torch.tensor((zoom * self.scenario.world_semidim) + self.scenario.render_origin[X], device=self.device),
                torch.tensor(-(zoom * self.scenario.world_semidim) + self.scenario.render_origin[Y], device=self.device),
                torch.tensor((zoom * self.scenario.world_semidim) + self.scenario.render_origin[Y], device=self.device),
            )
        elif shared_viewer:
            # zoom out to fit everyone
            all_poses = torch.stack(
                [agent.state.pos[env_index] for agent in self.world.agents], dim=0
            )
            max_agent_radius = max(
                [agent.shape.circumscribed_radius() for agent in self.world.agents]
            )
            viewer_size_fit = (
                torch.stack(
                    [
                        torch.max(
                            torch.abs(all_poses[:, X] - self.scenario.render_origin[X])
                        ),
                        torch.max(
                            torch.abs(all_poses[:, Y] - self.scenario.render_origin[Y])
                        ),
                    ]
                )
                + 2 * max_agent_radius
            )

            viewer_size = torch.maximum(
                viewer_size_fit / cam_range, torch.tensor(zoom, device=self.device)
            )
            cam_range *= torch.max(viewer_size)
            self.viewer.set_bounds(
                -cam_range[X] + self.scenario.render_origin[X],
                cam_range[X] + self.scenario.render_origin[X],
                -cam_range[Y] + self.scenario.render_origin[Y],
                cam_range[Y] + self.scenario.render_origin[Y],
            )
        else:
            # update bounds to center around agent
            pos = self.agents[agent_index_focus].state.pos[env_index]
            self.viewer.set_bounds(
                pos[X] - cam_range[X],
                pos[X] + cam_range[X],
                pos[Y] - cam_range[Y],
                pos[Y] + cam_range[Y],
            )

        # Render
        self._set_agent_comm_messages(env_index)

        if plot_position_function is not None:
            self.viewer.add_onetime(
                self.plot_function(
                    plot_position_function,
                    precision=plot_position_function_precision,
                    plot_range=plot_position_function_range,
                    cmap_range=plot_position_function_cmap_range,
                    cmap_alpha=plot_position_function_cmap_alpha,
                    cmap_name=plot_position_function_cmap_name,
                )
            )

        from vmas.simulator.rendering import Grid

        if self.scenario.plot_grid:
            grid = Grid(spacing=self.scenario.grid_spacing)
            grid.set_color(*vmas.simulator.utils.Color.BLACK.value, alpha=0.3)
            self.viewer.add_onetime(grid)

        self.viewer.add_onetime_list(self.scenario.extra_render(env_index))

        for entity in self.world.entities:
            self.viewer.add_onetime_list(entity.render(env_index=env_index))

        # render to display or array
        return self.viewer.render(return_rgb_array=mode == "rgb_array")

    def plot_function(
        self, f, precision, plot_range, cmap_range, cmap_alpha, cmap_name
    ):
        from vmas.simulator.rendering import render_function_util

        if plot_range is None:
            assert self.viewer.bounds is not None, "Set viewer bounds before plotting"
            x_min, x_max, y_min, y_max = self.viewer.bounds.tolist()
            plot_range = [x_min - precision, x_max - precision], [
                y_min - precision,
                y_max + precision,
            ]

        geom = render_function_util(
            f=f,
            precision=precision,
            plot_range=plot_range,
            cmap_range=cmap_range,
            cmap_alpha=cmap_alpha,
            cmap_name=cmap_name,
        )
        return geom

    def _init_rendering(self):
        from vmas.simulator import rendering

        self.viewer = rendering.Viewer(
            *self.scenario.viewer_size, visible=self.visible_display
        )

        self.text_lines = []
        idx = 0
        if self.world.dim_c > 0:
            for agent in self.world.agents:
                if not agent.silent:
                    text_line = rendering.TextLine(y=idx * 40)
                    self.viewer.geoms.append(text_line)
                    self.text_lines.append(text_line)
                    idx += 1

    def _set_agent_comm_messages(self, env_index: int):
        # Render comm messages
        if self.world.dim_c > 0:
            idx = 0
            for agent in self.world.agents:
                if not agent.silent:
                    assert (
                        agent.state.c is not None
                    ), "Agent has no comm state but it should"
                    if self.continuous_actions:
                        word = (
                            "["
                            + ",".join(
                                [f"{comm:.2f}" for comm in agent.state.c[env_index]]
                            )
                            + "]"
                        )
                    else:
                        word = ALPHABET[torch.argmax(agent.state.c[env_index]).item()]

                    message = agent.name + " sends " + word + "   "
                    self.text_lines[idx].set_text(message)
                    idx += 1

    @override(TorchVectorizedObject)
    def to(self, device: DEVICE_TYPING):
        device = torch.device(device)
        self.scenario.to(device)
        super().to(device)
