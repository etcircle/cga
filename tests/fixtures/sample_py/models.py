class Base:
    def base_method(self):
        return "base"


class Child(Base):
    def child_method(self):
        return self.base_method()
