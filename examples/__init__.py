# Regular package marker. Without it this directory is only a namespace
# package, and any OTHER package named `examples` earlier-or-regular on
# sys.path (e.g. automation_kit.pth -> E:/code/automation-kit/examples,
# which has __init__.py) shadows it -- regular packages beat namespace
# packages regardless of sys.path order. That made
# `from examples.balatro_real_replay import ...` resolve to the wrong
# tree on dev machines with automation-kit installed (CI unaffected).
