import gym
from pynput import keyboard

def on_key_release(key):
    global action
    try:
        if key.char == 'a':
            action = 3  # Move paddle left
        elif key.char == 'd':
            action = 2  # Move paddle right
    except AttributeError:
        pass

    if key == keyboard.Key.esc:
        global exit_program
        exit_program = True
        return False

if __name__ == "__main__":
    exit_program = False
    action = 0  # No action

    env = gym.make('Breakout-v4', render_mode='human')
    env.reset()

    with keyboard.Listener(on_release=on_key_release) as listener:
        while not exit_program:
            env.render()
            outputs = env.step(action)
            if len(outputs) == 5:
                observation, reward, done, info, extra = outputs
            else:
                print(f"Unexpected number of outputs: {len(outputs)}")
            if 1==2: #done:
                env.reset()
        listener.join()


    env.close()