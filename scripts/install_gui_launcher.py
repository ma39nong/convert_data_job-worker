"""Install a per-user application-menu launcher; optional desktop shortcut."""
import argparse
from pathlib import Path
import shutil


def main():
    p=argparse.ArgumentParser();p.add_argument('--desktop',action='store_true');p.add_argument('--take-over',action='store_true',help='正常停止并禁用旧的两个用户后台服务，改用 GUI 控制');args=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    if args.take_over:
        import subprocess,json,datetime
        units=['capture-convert-transfer.service','capture-normal-speedup.service'];before={}
        for unit in units:
            before[unit]={k:subprocess.run(['systemctl','--user',command,unit],capture_output=True,text=True).stdout.strip() for k,command in [('enabled','is-enabled'),('active','is-active')]}
        backup=root/'outputs/gui/legacy-service-backup.json';backup.parent.mkdir(parents=True,exist_ok=True)
        if not backup.exists():backup.write_text(json.dumps(before,indent=2)+'\n')
        for unit in units:
            if before[unit]['enabled'] not in ('not-found',''):
                subprocess.run(['systemctl','--user','stop',unit],check=True)
                subprocess.run(['systemctl','--user','disable',unit],check=True)
        print('旧服务已正常停止并禁用自启动；原配置和队列保留。备份：'+str(backup))
    target=Path.home()/'.local/share/applications/convert-data-workflow.desktop'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text('[Desktop Entry]\nType=Application\nName=数据工作流\nComment=ROSbag 清洗、LeRobot 转换与校验上传\n'
                      f'Exec="{root}/scripts/start_gui.sh"\nPath={root}\nTerminal=false\nIcon=folder-sync\nCategories=Development;Utility;\n')
    target.chmod(0o755);print(target)
    if args.desktop:
        desktop=next((p for p in [Path.home()/'桌面',Path.home()/'Desktop'] if p.is_dir()),Path.home()/'Desktop')
        desktop.mkdir(exist_ok=True);destination=desktop/'数据工作流.desktop'
        shutil.copy2(target,destination);print(destination)

if __name__=='__main__':main()
