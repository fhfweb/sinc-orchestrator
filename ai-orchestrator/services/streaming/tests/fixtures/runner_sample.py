import os


class Demo:
    def run(self, items=None):
        if items is None:
            items = []
        for item in items:
            print(item)
        return len(items)
