import { Controller, Get, Post } from "@nestjs/common";
import { UserService } from "./user.service";
@Controller("users")
export class UserController {
  constructor(private readonly service: UserService) {}
  @Get(":id")
  async findOne() {}
  @Post()
  async create() {}
}