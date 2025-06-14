// 메뉴 관련
export interface MenuItem {
  menuId: number
  menuName: string
  price: number
  categoryId: number
  categoryName: string
  isSoldOut: boolean
  imageUrl: string
  description: string
}

export interface MenuCardProps {
  item: MenuItem
  boxShadowColor: string
}

export interface AllMenuSectionProps {
  menuItems: MenuItem[]
  categories: Category[]
}

export interface RecommendSectionProps {
  recentMenus: MenuItem[]
  customMenus: MenuItem[]
}

// 메뉴 카테고리
export interface Category {
  categoryId: number
  categoryName: string
  displayOrder: number
  isActive: boolean
}

// 옵션 모달 관련
export interface MenuOption {
  optionCategory: string
  isRequired: boolean
  optionNames: string[]
  additionalPrices: number[]
  optionIds: number[]
  isDefault: boolean[]
  maxSelections: number
}

export interface MenuItemDetail {
  menuId: number
  menuName: string
  price: number
  categoryId: number
  categoryName: string
  isSoldOut: boolean
  imageUrl: string
  description: string
  options: MenuOption[]
}

export interface OptionModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCancel?: () => void
  onConfirm?: () => void
  menu: MenuItemDetail
}

// 선택 상품
export interface SelectedOption {
  category: string
  value: string
  price: number
  optionId: number
}

export interface OrderItem {
  menuId: number
  name: string
  basePrice: number
  quantity: number
  options: SelectedOption[]
  totalPrice: number
}
