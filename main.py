'''
@zihao:  
'''

from controller import *
from planner import *
from selector import *

from mineclip import MineCLIP
from transformers import CLIPProcessor, CLIPModel
from omegaconf import OmegaConf

import os
import json
import random
from datetime import datetime 
import time 

from typing import List, Dict, Tuple

from PIL import Image, ImageDraw
import cv2

import warnings
warnings.filterwarnings('ignore')

def resize_image_numpy(img, target_resolution = (128, 128)):
    img = cv2.resize(img, dsize=target_resolution, interpolation=cv2.INTER_LINEAR)
    return img

prefix = os.getcwd()
goal_mapping_json = os.path.join(prefix, "data/goal_mapping.json")
task_info_json = os.path.join(prefix, "data/task_info.json")
goal_lib_json = os.path.join(prefix, "data/goal_lib.json")
logging_folder = ""


# env_name = "crafting"
# task = "obtain_wooden_slab"     
task_list = [] 
with open(task_info_json, 'r') as f:
    task_info = json.load(f)
task_list = list(task_info.keys())

env = MineDojoEnv(
        name='Plains', 
        img_size=(640, 480),
        rgb_only=False,
    )

class Evaluator:
    def __init__(self, cfg, env):
        device = "cuda" if torch.cuda.is_available() else \
            ("mps" if torch.backends.mps.is_available() else "cpu")
        self.device = device
        self.cfg = cfg
        # super().__init__(cfg, device=device, only_base=True)
        self.num_workers = 0
        self.env = MineDojoEnv(
                name=cfg['eval']['env_name'], 
                img_size=(cfg['simulator']['resolution'][0], cfg['simulator']['resolution'][1]),
                rgb_only=False,
            )
        # self.env = env
        
        self.task_list = task_list
        
        self.use_ranking_goal = cfg["goal_model"]["use_ranking_goal"]
        
        self.goal_mapping_cfg = self.load_goal_mapping_config()
        self.mineclip_prompt_dict = self.goal_mapping_cfg["mineclip"]
        self.clip_prompt_dict = self.goal_mapping_cfg["clip"] # unify the mineclip and clip 
        self.goal_mapping_dct = self.goal_mapping_cfg["horizon"]

        print("[Progress] [red]Computing goal embeddings using MineClip's text encoder...")
        rely_goals = [val for val in self.goal_mapping_dct.values()]
        self.embedding_dict = accquire_goal_embeddings(cfg['pretrains']['clip_path'], rely_goals)
        
        self.goal_model_freq = cfg["goal_model"]["freq"]
        self.goal_list_size = cfg["goal_model"]["queue_size"]

        self.record_frames = cfg["record"]["frames"]
        
        self.mine_agent = MineAgent(cfg, device).model
        self.mine_wrapper = MineAgentWrapper(self.env, self.mine_agent, max_ranking=15)
        self.craft_agent = CraftAgent(self.env)
        self.planner = Planner()
        self.selector = Selector()

        task = cfg['eval']['task_name']
        self.reset(task)

       

    def reset(self, task):
        print(f"[INFO]: resetting the task {task}")
        self.planner.reset()
        self.task = task
        self.task_obj, self.max_ep_len, self.task_question, self.task_group = self.load_task_info(self.task)
        plan = self.planner.initial_planning(self.task_group, self.task_question)
        self.goal_list = self.planner.generate_goal_list(plan)
        if len(self.goal_list) == 0:
            self.curr_goal = {
                'name': 'mine_log', 
                'type': 'mine', 
                'object': {'log': 1}, 
                'precondition': {}, 
                'ranking': 1
            }
        else:
            self.curr_goal = self.goal_list[0]
        self.goal_eps = 0
        self.replan_rounds = 0
        self.logs = {}

    def load_task_info(self, task):
        with open(task_info_json, 'r') as f:
            task_info = json.load(f)
        target_item = task_info[task]['object']
        episode_length = int(task_info[task]["episode"])
        task_question = task_info[task]['question']
        task_group = task_info[task]['group']
        return target_item, episode_length, task_question, task_group

    def load_goal_mapping_config(self):
        with open(goal_mapping_json, "r") as f:
            goal_mapping_dct = json.load(f)
        return goal_mapping_dct 

    # check if the inventory has the object items
    def check_inventory(self, inventory, items:dict): # items: {"planks": 4, "stick": 2}
        for key in items.keys(): # check every object item 
            # item_flag = False
            if sum([item['quantity'] for item in inventory if item['name'] == key]) < items[key]:
                return False
        return True
    
    def check_precondition(self, inventory, precondition:dict): 
        for key in precondition.keys(): # check every object item 
            # item_flag = False
            if sum([item['quantity'] for item in inventory if item['name'] == key]) < precondition[key]:
                return False
        return True
    
    def check_done(self, inventory, task_obj:str):
        for item in inventory:
            if task_obj == item['name']:
                return True
        return False

    def update_goal(self, inventory):
        # while self.check_inventory(inventory, self.curr_goal["object"]):
        if self.check_inventory(inventory, self.curr_goal["object"]) and self.goal_eps>1:
            print(f"[INFO]: finish goal {self.curr_goal['name']}.")
            self.planner.generate_success_description(self.curr_goal["ranking"])
            self.goal_list.remove(self.goal_list[0])
            self.curr_goal = self.goal_list[0]
            self.goal_eps = 0

    def replan_task(self, inventory, task_question):
        self.planner.generate_failure_description(self.curr_goal['ranking'])
        self.planner.generate_inventory_description(inventory)
        self.planner.generate_explanation()
        plan = self.planner.replan(task_question)
        
        self.goal_list = self.planner.generate_goal_list(plan)
        if len(self.goal_list) == 0:
            self.curr_goal = {
                'name': 'mine_log', 
                'type': 'mine', 
                'object': {'log': 1}, 
                'precondition': {}, 
                'ranking': 1
            }
        else:
            self.curr_goal = self.goal_list[0]
        self.goal_eps = 0 
        self.replan_rounds += 1

    def logging(self, t):
        self.logs[t] = {}
        self.logs[t]['curr_plan'] = self.goal_list
        self.logs[t]['curr_goal'] = self.curr_goal
        self.logs[t]['curr_dialogue'] = self.planner.logging_dialogue


    @torch.no_grad()
    def eval_step(self, fps=200):
        
        self.mine_agent.eval()

        obs = self.env.reset() 

        # target_item = self.mapping_goal[goal]
        print(f"[INFO]: Evaluating the task is ", self.task)
        
        if self.record_frames:
            video_frames = [obs['rgb']]
            goal_frames = ["start"] 
        
        def preprocess_obs(obs: dict):
            res_obs = {}
            rgb = torch.from_numpy(obs['rgb']).unsqueeze(0).to(device=self.device, dtype=torch.float32).permute(0, 3, 1, 2)
            res_obs['rgb'] = resize_image(rgb, target_resolution=(120, 160))
            res_obs['voxels'] = torch.from_numpy(obs['voxels']).reshape(-1).unsqueeze(0).to(device=self.device, dtype=torch.long)
            res_obs['compass'] = torch.from_numpy(obs['compass']).unsqueeze(0).to(device=self.device, dtype=torch.float32)
            res_obs['gps'] = torch.from_numpy(obs['gps'] / np.array([1000., 100., 1000.])).unsqueeze(0).to(device=self.device, dtype=torch.float32)
            res_obs['biome'] = torch.from_numpy(obs['biome_id']).unsqueeze(0).to(device=self.device, dtype=torch.long)
            return res_obs

        def stack_obs(prev_obs: dict, obs: dict):
            stacked_obs = {}
            stacked_obs['rgb'] = torch.cat([prev_obs['rgb'], obs['rgb']], dim = 0)
            stacked_obs['voxels'] = torch.cat([prev_obs['voxels'], obs['voxels']], dim = 0)
            stacked_obs['compass'] = torch.cat([prev_obs['compass'], obs['compass']], dim = 0)
            stacked_obs['gps'] = torch.cat([prev_obs['gps'], obs['gps']], dim = 0)
            stacked_obs['biome'] = torch.cat([prev_obs['biome'], obs['biome']], dim = 0)
            return stacked_obs

        def slice_obs(obs: dict, slice: torch.tensor):
            res = {}
            for k, v in obs.items():
                res[k] = v[slice]
            return res

        def add_obs(video, image):
            video = np.concatenate((video, image.reshape(1, 1, 3, 160, 256)), axis = 1)
            if video.shape[1] > self.clip_frames:
                video = video[:,1:,:,:,:]
            return video
        
        obs = preprocess_obs(obs)

        states = obs
        actions = torch.zeros(1, self.mine_agent.action_dim, device=self.device)

        acquire = []
        curr_goal = None
        prev_goal = None
        seek_point = 0
        history_gps = []
        
        obs, reward, env_done, info = self.env.step(self.env.action_space.no_op())
        init_deaths = info['stat']['deaths']

        now = datetime.now()
        timestamp = f"{now.year}_{now.month}_{now.day}_{now.hour}_{now.minute}_{now.second}_"
        log_folder_name = os.path.join(prefix, "logs/")
        if not os.path.exists(log_folder_name):
            os.mkdir(log_folder_name)
        log_file_name = log_folder_name + timestamp + self.task + '.json'
        with open(log_file_name, 'w') as f:
            json.dump(self.logs, f, indent=4)
        
        # max_ep_len = task_eps[self.task]
        for t in range(0, self.max_ep_len):
            time.sleep(1/fps)

            self.update_goal(info['inventory'])
            curr_goal = self.curr_goal

            if not prev_goal == curr_goal:
                print(f"[INFO]: Episode Step {t}, Current Goal {curr_goal}")
                seek_point = t
                actions = torch.zeros(actions.shape[0], self.mine_agent.action_dim, device=self.device)
                self.logging(t)
                with open(log_file_name, 'w') as f:
                    json.dump(self.logs, f, indent=4)
            prev_goal = curr_goal

             # take the current goal type
            curr_goal_type = self.curr_goal["type"]
            
            sf = self.cfg['data']['skip_frame']
            wl = self.cfg['data']['window_len']
            
            end = actions.shape[0] - 1
            rg = torch.arange(end, min(max(end-sf*(wl-1)-1, seek_point-1), end-1), -sf).flip(0)

            # DONE: change the craft agent into craft actions
            if curr_goal_type in ['craft', 'smelt']:
                action_done = False
                preconditions = self.curr_goal["precondition"].keys()
                goal = list(self.curr_goal['object'].keys())[0]
                curr_actions, action_done = self.craft_agent.get_action(preconditions, curr_goal_type, goal)

            elif curr_goal_type == "mine":
                action_done = True
                goal = self.goal_mapping_dct[list(self.curr_goal["object"].keys())[0]]
                goal_embedding = self.embedding_dict[goal]
                goals = torch.from_numpy(goal_embedding).to(self.device).repeat(len(rg), 1)
                complete_states = slice_obs(states, rg)
                complete_states['prev_action'] = actions[rg]
                

                _ranking, _action = self.mine_wrapper.get_action(goal, goals, complete_states)
                curr_actions = _action
            else:
                print("Undefined action type !!")
            
            if len(self.curr_goal['precondition'].keys()):
                for cond in self.curr_goal['precondition'].keys():
                    if cond not in ['wooden_pickaxe', 'stone_pickaxe', 'iron_pickaxe', "diamond_pickaxe", 
                                "wooden_axe", "stone_axe", "iron_axe", "diamond_axe"]:
                        continue
                    if info['inventory'][0]['name'] != cond:
                        for item in info['inventory']:
                            if item['name'] == cond and item['quantity'] > 0 and item['index'] > 0:
                                act = self.env.action_space.no_op()
                                act[5] = 5
                                act[7] = item['index']
                                self.env.step(act)
                                break
            #! indent change
            action = curr_actions
            if torch.is_tensor(action):
                action = action.cpu().numpy()
            obs, reward, env_done, info = self.env.step(action)

            if self.record_frames:
                video_frames.append(obs['rgb'])
                goal_frames.append(curr_goal['name'])
            obs = preprocess_obs(obs)

            if type(action) != torch.Tensor:
                action = torch.from_numpy(action)
            if action.device != self.device:
                action = action.to(self.device)

            states = stack_obs(states, obs)
            actions = torch.cat([actions, action.unsqueeze(0)], dim = 0)

            self.goal_eps += 1
            if curr_goal_type == 'mine' and not self.check_precondition(info['inventory'], self.curr_goal["precondition"]):
                self.replan_task(info["inventory"], self.task_question)
                self.logging(t)
                with open(log_file_name, 'w') as f:
                    json.dump(self.logs, f, indent=4)
            elif curr_goal_type == 'craft' and self.goal_eps > 150:
                self.replan_task(info["inventory"], self.task_question)
                self.logging(t)
                with open(log_file_name, 'w') as f:
                    json.dump(self.logs, f, indent=4)
            elif curr_goal_type == 'smelt' and self.goal_eps > 200:
                self.replan_task(info["inventory"], self.task_question)
                self.logging(t)
                with open(log_file_name, 'w') as f:
                    json.dump(self.logs, f, indent=4)

            if self.replan_rounds > 12:
                print("[INFO]: replanning over rounds")
                break
            
            if self.check_done(info['inventory'], self.task_obj):  # check if the task is done?
                env_done = True
                print(f"[INFO]: finish goal {self.curr_goal['name']}.")
                self.planner.generate_success_description(self.curr_goal["ranking"])
                self.logs[t] = {}
                self.logs[t]['curr_plan'] = self.goal_list
                self.logs[t]['curr_goal'] = self.curr_goal
                self.logs[t]['curr_dialogue'] = self.planner.logging_dialogue
                self.logs[t]['result'] = True
                break

        # record the video
        if env_done and self.record_frames:
        # if self.record_frames:
            print("[INFO]: saving the frames")
            imgs = []
            for id, frame in enumerate(video_frames):
                frame = resize_image_numpy(frame, (320,240)).astype('uint8')
                cv2.putText(
                    frame,
                    f"FID: {id}",
                    (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (255, 255, 255),
                    2,
                )
                cv2.putText(
                    frame,
                    f"Goal: {goal_frames[id]}",
                    (10, 55),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (255, 0, 0), 
                    2,
                )
                imgs.append(Image.fromarray(frame))
            imgs = imgs[::3]
            print(f"record imgs length: {len(imgs)}")
            now = datetime.now()
            timestamp = f"{now.year}_{now.month}_{now.day}_{now.hour}_{now.minute}_{now.second}"
            folder_name = os.path.join(prefix, "recordings/"+timestamp+"/")
            if not os.path.exists(folder_name):
                os.mkdir(folder_name)
            imgs[0].save(folder_name+self.task + ".gif", save_all=True, append_images=imgs[1:], optimize=False, quality=0, duration=150, loop=0)
            with open(folder_name+self.task + ".json", 'w') as f:
                json.dump(self.logs, f, indent=4)
        
        return env_done, t # True or False, episode length

    def single_task_evaluate(self):
        loops = self.cfg['eval']['goal_ratio']
        if self.num_workers == 0:
            succ_rate = 0
            episode_lengths = []
            for i in range(loops):
                try:
                    self.reset(self.task)
                    succ_flag, min_episode = self.eval_step()
                except Exception as e:
                    print(e)
                    succ_flag = False
                    min_episode = 0
                succ_rate += succ_flag
                if succ_flag: 
                    episode_lengths.append(min_episode)
                print(f"Task {self.task} | Iteration {i} | Successful {succ_flag} | Episode length {min_episode} | Success rate {succ_rate/(i+1)}")
            print("success rate: ", succ_rate/loops)
            print("average episode length:", sum(episode_lengths)/(len(episode_lengths)+0.01))


@hydra.main(config_path="configs", config_name="defaults")
def main(cfg):
    print(cfg)
    evaluator = Evaluator(cfg, env) 
    evaluator.single_task_evaluate()


if __name__ == '__main__':
    main()
