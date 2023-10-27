import torch
from torch.nn import ModuleDict
import time
import os
import xml.etree.ElementTree as ET
from PIL import Image
from vmas import make_env

def select_action(position, waypoint):    
    x_mag = waypoint[0] - position[0]
    y_mag = waypoint[1] - position[1]
    if abs(x_mag) > abs(y_mag):
        if x_mag < 0:
            return [1]
        else:
            return [2]
    else:
        if y_mag < 0:
            return [3]
        else:
            return [4]

def get_agent_action(episodes, obs, steps):
    scenario_time = steps / 40.0 #TODO this needs to be better
    actions = []
    for i, ob in enumerate(obs.values()):
        env_actions = []
        for env_ob in ob:
            episode = episodes[int(env_ob[0])]
            x, y = env_ob[1:3]
            appended = False
            for waypoint in episode[i]:
                if waypoint[0] > scenario_time:
                    env_actions.append(select_action([x,y],waypoint[1:]))
                    appended = True
                    break
            if not appended:
                env_actions.append([0])
        actions.append(env_actions)
        
    return actions


device = "cpu"  # or cuda or any other torch device

#Load in the config to get agent sizes and the time limit
f = os.path.dirname(__file__)
config = ET.parse(f'{f}/config.xml')
config_root = config.getroot()
agent_size = float(config_root.find('.//agent_size').text)
time_limit = int(config_root.find('.//timelimit').text)

#Load in all of the pregenerated episodes
episodes = {}
for file in os.listdir(f'{f}/logs/'):
    if file[-3:] != 'xml':
        continue

    #episode name must be a number!
    episode_name = int(file[:-4].split('_')[-1])
    
    agent_paths = {}
    log = ET.parse(f'{f}/logs/{file}')
    log_root = log.getroot()
    agents = log_root.findall(".//agent[@number]")
    for agent in agents:
        number = int(agent.get('number'))
        waypoints = []
        path = agent.find('.//path')
        sections = path.findall('.//section')
        first = True
        for waypoint in sections:
            if first:
                x = int(waypoint.get('start_i'))
                y = int(waypoint.get('start_j'))
                duration = 0.0
                waypoints.append(torch.tensor([duration, x, y], device=device))
                first=False
            x = int(waypoint.get("goal_i"))
            y = int(waypoint.get('goal_j'))
            duration = float(waypoint.get('duration')) + waypoints[-1][0]
            waypoints.append(torch.tensor([duration, x, y], device=device))
        agent_paths[number] = waypoints
    episodes[episode_name] = agent_paths.copy()

scenario_name = 'navigation2' #Scenario name

# Scenario specific variables
n_agents = len(episodes[next(iter(episodes))].keys()) #Assumes all episodes has same number of agents

num_envs = 5  # Number of vectorized environments
continuous_actions = False
n_steps = 360  # Number of steps before returning done
dict_spaces = True  # Weather to return obs, rewards, and infos as dictionaries with agent names (by default they are lists of len # of agents)

simple_2d_action = (
    [0, 0.5] if continuous_actions else [3]
)  # Simple action tell each agent to go down
obs = []
env = make_env(
    scenario=scenario_name,
    num_envs=num_envs,
    device=device,
    continuous_actions=continuous_actions,
    dict_spaces=dict_spaces,
    wrapper=None,
    seed=None,
    # Environment specific variables
    n_agents=n_agents,
    episodes = episodes
)

frame_list = []  # For creating a gif
init_time = time.time()
step = 0
actions = {} 
for s in range(n_steps):
    step += 1
    print(f"Step {step}")
    
    if len(obs) == 0:
        agent_actions = [[simple_2d_action] * num_envs]* len(env.agents)
    else:
        agent_actions = get_agent_action(episodes, obs, step)

    for i, agent in enumerate(env.agents):
        action = torch.tensor(
            agent_actions[i],
            device=device,
        )#.repeat(num_envs, 1)
        actions.update({agent.name: action})

    obs, rews, dones, info = env.step(actions)

    frame_list.append(
        Image.fromarray(env.render(mode="rgb_array", agent_index_focus=None))
    )

total_time = time.time() - init_time
print(
    f"It took: {total_time}s for {n_steps} steps of {num_envs} parallel environments on device {device} "
    f"for {scenario_name} scenario."
)

gif_name = scenario_name + ".gif"
# Produce a gif
frame_list[0].save(
    gif_name,
    save_all=True,
    append_images=frame_list[1:],
    duration=3,
    loop=0,
)

